"""Persist a strategy's completed bars in the historical ``Backtest_Bar`` layout.

A strategy may emit a ``BAR`` metric whose tags identify the market and whose
fields contain both the bar and its indicators.  ``DatalakeReporter`` copies that
metric to the research layer of the data lake, in the layout earlier tools
wrote, so existing readers keep working.

One file is written per strategy, market, parameter set and local calendar day::

    research/dataset=Backtest_Bar/strategy=<strategy>/exchange=<exchange>/
      symbol=<symbol>/param_id=<param id>/date=YYYY-MM-DD/data.parquet

The metric fields are deliberately not interpreted.  This keeps strategy
indicator names such as ``sar:up``, ``sar:s`` and ``sar:ns`` unchanged and
makes the physical Parquet columns match the former writer.
"""

from datetime import datetime
from logging import getLogger
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from qate.core.model import ExchangeName, Field, Measurement, Metric, Tag
from qate.core.symbol import Symbol
from qate.trading.reporter import Reporter
from qate.util.dt_range import DtRange

LOG = getLogger(__name__)

DATASET = "Backtest_Bar"
JST = ZoneInfo("Asia/Tokyo")


def _pairs(flat: list) -> list[tuple]:
    if len(flat) % 2:
        raise ValueError(f"A metric's key/value list has odd length {len(flat)}")
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]


def _field_name(key) -> str:
    return Field(key).name if type(key) is int else str(key)


def _market(metric: Metric) -> tuple[str, str]:
    tags = dict(_pairs(metric.tags))
    exchange = tags.get(Tag.EXCHANGE_NAME.value, tags.get(Tag.EXCHANGE_NAME.name))
    symbol = tags.get(Tag.SYMBOL.value, tags.get(Tag.SYMBOL.name))
    if exchange is None or symbol is None:
        raise ValueError("A BAR metric must have EXCHANGE_NAME and SYMBOL tags")
    if not isinstance(exchange, ExchangeName):
        exchange = ExchangeName(exchange)
    if not isinstance(symbol, Symbol):
        symbol = Symbol(symbol)
    return exchange.name, symbol.name


class DatalakeReporter(Reporter):
    """Write in-range ``BAR`` metrics to daily, Zstandard-compressed Parquet files."""

    def __init__(
        self,
        root: str | Path,
        strategy_name: str,
        param_set_id: str,
        dt_range: DtRange,
    ):
        super().__init__()
        self.root = Path(root)
        self.strategy_name = strategy_name
        self.param_set_id = param_set_id
        self._start_ts = dt_range.start_ts
        self._end_ts = dt_range.end_ts
        self._buffers: dict[tuple[str, str, str], list[dict]] = {}
        self._current_date: dict[tuple[str, str], str] = {}

    def _in_range(self, ts: float) -> bool:
        return self._start_ts <= ts <= self._end_ts

    @staticmethod
    def _date(ts: float) -> str:
        # Data-lake date partitions are local calendar days in JST, the
        # partitioning the existing files already use.
        return datetime.fromtimestamp(ts, JST).date().isoformat()

    def report_metrics(self, metrics: list[Metric]) -> None:
        for metric in metrics:
            if metric.measurement != Measurement.BAR.value or not self._in_range(metric.ts):
                continue
            exchange, symbol = _market(metric)
            fields = {_field_name(key): value for key, value in _pairs(metric.fields)}
            # BAR fields historically contain their own ``ts`` (the completed
            # bar's close_ts).  Its value replaces the event timestamp while
            # retaining ``ts`` as the first physical column.
            record = {"ts": metric.ts, **fields}
            market = (exchange, symbol)
            date = self._date(metric.ts)
            previous = self._current_date.get(market)
            if previous is not None and previous != date:
                self._flush(exchange, symbol, previous)
            self._current_date[market] = date
            key = (exchange, symbol, date)
            self._buffers.setdefault(key, []).append(record)

    def _path(self, exchange: str, symbol: str, date: str) -> Path:
        return (
            self.root
            / "research"
            / f"dataset={DATASET}"
            / f"strategy={self.strategy_name}"
            / f"exchange={exchange}"
            / f"symbol={symbol}"
            / f"param_id={self.param_set_id}"
            / f"date={date}"
            / "data.parquet"
        )

    def _flush(self, exchange: str, symbol: str, date: str) -> None:
        records = self._buffers.pop((exchange, symbol, date), None)
        if not records:
            return
        path = self._path(exchange, symbol, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(records)
        table = pa.Table.from_pandas(frame, preserve_index=False)
        pq.write_table(table, path, compression="zstd")
        LOG.info(f"Saved {len(records)} bars to {path}")

    def stop(self) -> None:
        for exchange, symbol, date in list(self._buffers):
            self._flush(exchange, symbol, date)
        self._current_date.clear()
