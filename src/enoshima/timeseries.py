"""Hive-partitioned parquet time series, written and read locally.

One dataset, one partition path, one file per day:

    <root>/dataset=<dataset>/<key>=<value>/.../date=YYYY-MM-DD/data.parquet

That is the layout PyArrow, DuckDB, Spark and Athena all recover partition keys
from, so a whole run is one `read_parquet` with a glob and there is no index to
keep in step. It is also the layout the Kamakura Quant Lab bronze data arrives
in, so a run's own output sits beside the data it was computed from and reads
with the same tools.

The class is the generic half. What a backtest actually writes -- per-trade PnL
and strategy bars -- is `ResultStore` at the bottom of this module.

## Why this is here and not in qate

Writing results means choosing a storage layout, and a layout belongs to whoever
writes it. A backtest is what produces results, so the layout is Enoshima's. `qate`
owns the model and the machinery and no longer owns any storage at all -- the same
reason `bronze.py` reads here rather than there.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from logging import getLogger
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from qate.core.model import Market
from qate.util.dt_range import DtRange, TimeFormat

LOG = getLogger(__name__)


class TimeSeriesStore(ABC):
    """Append rows, rotate on the date partition, flush to parquet.

    Subclasses declare their partition keys; everything else is buffering.
    """

    def __init__(self, root_dir: str | Path, dataset: str):
        self.root_dir = Path(root_dir)
        self.dataset = dataset
        self._buffer: list[dict] = []
        self._cols: list[str] | None = None
        self._current_time_key: str | None = None

    @abstractmethod
    def get_partition_keys(self) -> dict[str, str]:
        pass

    def get_time_partition_key(self, ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime(TimeFormat.ISO_DATE)

    def _get_data_dir(self, time_key: str) -> Path:
        path = self.root_dir / f"dataset={self.dataset}"
        for key, val in self.get_partition_keys().items():
            path = path / f"{key}={val}"
        return path / f"date={time_key}"

    def _get_parquet_path(self, time_key: str) -> Path:
        return self._get_data_dir(time_key) / "data.parquet"

    def add(self, ts: float, data: dict) -> None:
        time_key = self.get_time_partition_key(ts)

        if self._current_time_key is None:
            self._current_time_key = time_key

        if time_key != self._current_time_key:
            self.flush()
            self._current_time_key = time_key

        self._buffer.append({"ts": ts, **data})

    def flush(self) -> None:
        if not self._buffer:
            return
        parquet_path = self._get_parquet_path(self._current_time_key)
        parquet_path.parent.mkdir(parents=True, exist_ok=True)

        df = pd.DataFrame(self._buffer, columns=self._cols) if self._cols else pd.DataFrame(self._buffer)
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, parquet_path, compression="zstd")
        LOG.info(f"Flushed {len(self._buffer)} records to {parquet_path}")
        self._buffer.clear()

    def read_a_day(self, date_str: str) -> pd.DataFrame:
        parquet_path = self._get_parquet_path(date_str)
        if not parquet_path.exists():
            return pd.DataFrame()
        try:
            df = pd.read_parquet(parquet_path)
        except Exception as e:
            LOG.warning(f"Failed to read {parquet_path}: {e}")
            return pd.DataFrame()
        LOG.info(f"Loaded {len(df)} records from {parquet_path}")
        return df

    def read_days(self, days: list[str]) -> pd.DataFrame:
        frames = [df for df in (self.read_a_day(day) for day in days) if not df.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def read(self, date_arg: DtRange | list[str]) -> pd.DataFrame:
        """Every row for a range or an explicit list of partition dates.

        A `DtRange` is trimmed to its exact timestamps; the day partitions only
        get us to the right files.
        """
        if isinstance(date_arg, DtRange):
            result = self.read_days(date_arg.days)
            if not result.empty:
                result = result[(result["ts"] >= date_arg.start_ts) & (result["ts"] <= date_arg.end_ts)]
        elif isinstance(date_arg, list):
            result = self.read_days(date_arg)
        else:
            raise ValueError("date_arg must be a DtRange or a list of date strings")

        if result.empty:
            return result
        return result.sort_values("ts").reset_index(drop=True)

    def data_check(self, dt_range: DtRange) -> dict:
        """Which partitions of a range are on disk, and how big they are."""
        missing, sizes = [], []
        for day in dt_range.days:
            path = self._get_parquet_path(day)
            if path.exists():
                sizes.append(path.stat().st_size)
            else:
                missing.append(day)

        return {
            "num_days": len(dt_range.days),
            "num_files": len(sizes),
            "missing_files": missing,
            "avg_size": int(sum(sizes) / len(sizes)) if sizes else 0,
            "max_size": max(sizes) if sizes else 0,
            "min_size": min(sizes) if sizes else 0,
        }

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "TimeSeriesStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False


class SingleMarketStore(TimeSeriesStore):
    """Partitioned by exchange and symbol: `exchange=GMO/symbol=BTC_JPY`."""

    def __init__(self, root_dir: str | Path, dataset: str, market: Market):
        super().__init__(root_dir, dataset)
        self.market = market

    def get_partition_keys(self) -> dict[str, str]:
        return {
            "exchange": self.market.exchange_name.name,
            "symbol": self.market.symbol.name,
        }


class StrategyStore(SingleMarketStore):
    """A single market's output for one strategy and one parameter set.

    `param_set_id` last in the path so that every combination of one optimize
    run sits side by side under the same strategy prefix.
    """

    def __init__(self, root_dir: str | Path, dataset: str, market: Market, strategy_name: str, param_set_id: str):
        super().__init__(root_dir, dataset, market)
        self.strategy_name = strategy_name
        self.param_set_id = param_set_id

    def get_partition_keys(self) -> dict[str, str]:
        return {
            "strategy": self.strategy_name,
            **super().get_partition_keys(),
            "param_id": self.param_set_id,
        }


class ResultStore:
    """Where one backtest's own output goes, under a local directory.

    Results are a run's own artefact, so the root is wherever the caller keeps
    the run -- an env directory, a scratch path -- and nothing here consults a
    warehouse or a shared location.
    """

    PNL = "Pnl"
    BAR = "Bar"

    def __init__(self, root_dir: str | Path, strategy_name: str, param_set_id: str):
        self.root_dir = Path(root_dir)
        self.strategy_name = strategy_name
        self.param_set_id = param_set_id

    def create_pnl(self, market: Market) -> StrategyStore:
        return StrategyStore(self.root_dir, self.PNL, market, self.strategy_name, self.param_set_id)

    def create_bar(self, market: Market) -> StrategyStore:
        return StrategyStore(self.root_dir, self.BAR, market, self.strategy_name, self.param_set_id)
