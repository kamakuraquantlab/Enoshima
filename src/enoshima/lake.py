"""Where a run's results live: the data lake's `research/` layer, beside the bronze it replayed.

A backtest reads its market data from the lake, so it writes what it computed
back to it, as Hive-partitioned Parquet like every other dataset there, and
the environment directory holds definitions and `env.log`, not results.

    research/dataset=BacktestRun/env=<env>/run_id=<id>/data.parquet
    research/dataset=BacktestPart/env=<env>/run_id=<id>/part=<part>/data.parquet
    research/dataset=BacktestTrade/env=<env>/run_id=<id>/part=<part>/data.parquet
    research/dataset=BacktestOrder/...                   the same partitions
    research/dataset=BacktestPnl/...
    research/dataset=BacktestBar/...
    research/dataset=BacktestMetric/...


| dataset | one row per | from |
|---|---|---|
| `BacktestRun` | run: its provenance and its whole-range summary | `meta`, `summary` |
| `BacktestPart` | part of a run: the same, for that part | `meta`, `summary` |
| `BacktestTrade` | closed position (`ledger.Trade`) | `ResultReporter` |
| `BacktestOrder` | fill (qate's `ORDER` record) | `records.RecordReporter` |
| `BacktestPnl` | PnL update, open and close (qate's `PNL`, with `open_ts`) | `records.RecordReporter` |
| `BacktestBar` | completed bar and its indicators (the strategy's `BAR`) | `records.RecordReporter` |
| `BacktestMetric` | any other metric, with its `measurement` (`EVAL` among them) | `records.RecordReporter` |

## Why `research`

The lake's other layers are the market's: bronze as recorded, silver and gold
derived from it, the same for every reader. A backtest's output is an
experiment's -- one strategy, one revision, one parameter set -- and is
rebuilt by replaying, which is what `research/` is for and where the lake's
earlier backtest outputs already are. They are named `Backtest_Bar` and
`Backtest_Pnl` and partitioned by strategy and market; the names here differ
so that the two layouts never share a dataset directory.

## The partitions are the run's id

`run_id` is `run_log.BacktestLog.get_id`, the hash of the env, the strategy
and its variant, the variant's source revision, the parameters, the non-zero
fee rates and the range -- or, weekly, the preload and overlap. Every row of
every dataset is under its env and run id, so two runs that would compute
different numbers never share a file, and `BacktestRun` holds what the hash
was made of (`strategy`, `strategy_rev`, `param_set_id`, `params`, ...) to
join on. Readers should not let DuckDB autocast partition values: a run id
of sixteen digits is a hex string, not a number.

`part` is what a run writes at once: an ISO week (`2026-W41`) of a weekly run,
`all` for a temporary one. A part is written whole, its data files first and
its `BacktestPart` row last, and only that row says it is finished; the
run's `BacktestRun` row is rebuilt from its parts after (`weekly.rebuild`) and
is what "the run is complete" means to `results`, `enoshima web` and any reader of the lake.
A part's row records how many rows each of its files holds, and a reader
refuses a part whose files are not all there: a file removed from under a
finished part would otherwise read as a part that traded nothing.

## A row's columns

`BacktestRun` and `BacktestPart` hold `meta` and `summary` as JSON text, which
is what Enoshima reads back, and beside them every scalar of both as a column
of its own and every nested meta value (`params`, `config`, ...) as JSON text,
so the view answers `SELECT run_id, pnl5 FROM backtestrun WHERE env = ...`
without parsing. `BacktestRun` also holds the run's trades grouped by each of
`ledger.GROUPINGS`, as `by_<name>` JSON -- what the folder's `by_*.json` were:
a reader comparing every run's months need not read trades that run to millions. The record datasets' columns are their metrics' tag and field
names; `BacktestBar`'s are whatever the strategy put in its bars. Which of
them were tags is in each file's Parquet metadata (`records.TAGS_KEY`).
"""

import json
import os
import shutil
from dataclasses import astuple
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from qate.util.encoder import Encoder

from .ledger import GROUPINGS, TRADE_COLUMNS, Trade, aggregate

LAYER = "research"
RUN = "BacktestRun"
PART = "BacktestPart"
TRADE = "BacktestTrade"
ORDER = "BacktestOrder"
PNL = "BacktestPnl"
BAR = "BacktestBar"
METRIC = "BacktestMetric"
RECORDS = (ORDER, PNL, BAR, METRIC)
DATA = (TRADE, *RECORDS)
DATASETS = (RUN, PART, *DATA)

WHOLE = "all"  # the one part of a temporary run
FILE = "data.parquet"

_TRADE_SCHEMA = pa.schema(
    [(name, pa.string() if name in ("exchange", "symbol", "side") else pa.float64()) for name in TRADE_COLUMNS]
)


class IncompletePart(ValueError):
    """A finished part whose files are not all there."""


def lake_root() -> Path:
    """The data lake: hase's answer, which is komachi's."""
    from hase import bronze

    return Path(bronze.data_root())


def envs(root: Path) -> list[str]:
    """Every environment with a finished run in the lake under `root`."""
    base = Path(root) / LAYER / f"dataset={RUN}"
    if not base.is_dir():
        return []
    return sorted(
        p.name.split("=", 1)[1]
        for p in base.glob("env=*")
        if p.is_dir() and any((r / FILE).is_file() for r in p.glob("run_id=*"))
    )


def _dumps(value) -> str:
    return json.dumps(value, cls=Encoder)


def _plain(value):
    """`value` as JSON would have it: dataclasses and enums as the `Encoder` writes them."""
    return json.loads(_dumps(value))


def _write_atomic(path: Path, table: pa.Table, row: bool = False) -> None:
    """`row` for a one-row table: no statistics or dictionaries, which made it 2.4 times the size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    if row:
        pq.write_table(table, tmp, compression="zstd", write_statistics=False, use_dictionary=False)
    else:
        pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


def _read(path: Path, columns: list[str] | None = None) -> pa.Table:
    # `ParquetFile`, not `read_table`: the latter adds the Hive partitions of the path as columns.
    return pq.ParquetFile(path).read(columns=columns)


def _column(values: list) -> pa.Array:
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        return pa.array([None if v is None else str(v) for v in values], pa.string())


def table_of(rows: dict[str, list]) -> pa.Table | None:
    """Columns of equal length as a table, each typed from its values; None for no rows."""
    if not rows or not len(next(iter(rows.values()))):
        return None
    return pa.table({name: _column(values) for name, values in rows.items()})


def _row(meta: dict, summary: dict, extra: dict | None = None) -> pa.Table:
    """One `BacktestRun` or `BacktestPart` row: see the module's note on columns."""
    meta, summary = _plain(meta), _plain(summary)
    columns: dict[str, pa.Array] = {}

    def put(name, value, null_type):
        if name in columns:
            return
        if isinstance(value, (dict, list)):
            columns[name] = pa.array([_dumps(value)], pa.string())
        elif value is None:
            columns[name] = pa.array([None], null_type)
        else:
            columns[name] = _column([value])

    for name, value in meta.items():
        put(name, value, pa.string())
    for name, value in summary.items():
        put(name, value, pa.float64())
    for name, value in (extra or {}).items():
        put(name, value, pa.string())
    columns["meta"] = pa.array([_dumps(meta)], pa.string())
    columns["summary"] = pa.array([_dumps(summary)], pa.string())
    return pa.table(columns)


def _unrow(table: pa.Table) -> dict:
    row = table.slice(0, 1).to_pylist()[0]
    return {"meta": json.loads(row["meta"]), "summary": json.loads(row["summary"]),
            "counts": json.loads(row["counts"]) if row.get("counts") else {}}


def trades_table(trades: list[Trade]) -> pa.Table:
    rows = [astuple(t) for t in trades]
    return pa.table({name: [r[i] for r in rows] for i, name in enumerate(TRADE_COLUMNS)}, schema=_TRADE_SCHEMA)


def trades_from(table: pa.Table) -> list[Trade]:
    names = set(table.column_names)
    columns = [table.column(c).to_pylist() if c in names else [None] * table.num_rows for c in TRADE_COLUMNS]
    return [Trade(*row) for row in zip(*columns)]


def groupings_of(trades: list[Trade]) -> dict[str, dict]:
    """Every `ledger.GROUPINGS` of a run's trades, by name: what `by_<name>.json` was."""
    return {name: aggregate(trades, name) for name in GROUPINGS}


class Store:
    """One environment's results in the lake under `root`."""

    def __init__(self, root: Path, env: str):
        self.root = Path(root)
        self.env = str(env)

    def __eq__(self, other) -> bool:
        return isinstance(other, Store) and (self.root, self.env) == (other.root, other.env)

    def __hash__(self) -> int:
        return hash((self.root, self.env))

    @classmethod
    def of(cls, env: str) -> "Store":
        return cls(lake_root(), env)

    # ------------------------------------------------------------- paths

    def dataset_dir(self, dataset: str) -> Path:
        return self.root / LAYER / f"dataset={dataset}" / f"env={self.env}"

    def run_dir(self, dataset: str, run_id: str) -> Path:
        return self.dataset_dir(dataset) / f"run_id={run_id}"

    def path(self, dataset: str, run_id: str, part: str | None = None) -> Path:
        base = self.run_dir(dataset, run_id)
        return (base if part is None else base / f"part={part}") / FILE

    # ------------------------------------------------------------- writing

    def write_part(self, run_id: str, part: str, meta: dict, summary: dict, trades: list[Trade],
                   records: dict[str, pa.Table | None] | None = None) -> Path:
        """Write one part whole, replacing any earlier one: its data files, then its row.

        The part's row, and the run's, are removed first: until the new row is
        written the part is not finished, rather than finished with other data.
        """
        self.path(PART, run_id, part).unlink(missing_ok=True)
        self.path(RUN, run_id).unlink(missing_ok=True)
        tables = {TRADE: trades_table(trades), **(records or {})}
        counts = {}
        for dataset in DATA:
            table = tables.get(dataset)
            path = self.path(dataset, run_id, part)
            if table is None or table.num_rows == 0:
                path.unlink(missing_ok=True)  # an earlier write's, not this part's
                counts[dataset] = 0
                continue
            _write_atomic(path, table)
            counts[dataset] = table.num_rows
        path = self.path(PART, run_id, part)
        _write_atomic(path, _row(meta, summary, {"counts": counts}), row=True)
        return path

    def write_run(self, run_id: str, meta: dict, summary: dict, trades: list[Trade]) -> Path:
        """The run's row, from its meta, its summary and every part's trades, for the groupings."""
        path = self.path(RUN, run_id)
        groupings = {f"by_{name}": table for name, table in groupings_of(trades).items()}
        _write_atomic(path, _row(meta, summary, groupings), row=True)
        return path

    def remove_run(self, run_id: str) -> None:
        """Mark a run not complete. Its parts are kept; the run's row is rebuilt from them."""
        self.path(RUN, run_id).unlink(missing_ok=True)

    def delete_run(self, run_id: str) -> None:
        """Every file of a run, in every dataset. Only `enoshima web`'s delete, when asked."""
        for dataset in DATASETS:
            shutil.rmtree(self.run_dir(dataset, run_id), ignore_errors=True)

    def rename_run(self, run_id: str, new_id: str) -> None:
        """Move a run's files, in every dataset, to `new_id`. Its rows are the caller's to rewrite."""
        for dataset in DATASETS:
            source = self.run_dir(dataset, run_id)
            if source.is_dir():
                target = self.run_dir(dataset, new_id)
                if target.exists():
                    raise FileExistsError(f"{target} already exists")
                source.rename(target)

    def write_part_meta(self, run_id: str, part: str, meta: dict) -> None:
        """Replace a finished part's meta, keeping its summary and files."""
        found = self.read_part(run_id, part)
        _write_atomic(self.path(PART, run_id, part), _row(meta, found["summary"], {"counts": found["counts"]}),
                      row=True)

    def copy_part(self, run_id: str, part: str, target_id: str, meta: dict) -> None:
        """Copy one finished part to another run, with `meta` as its row's meta."""
        found = self.read_part(run_id, part)
        self.path(PART, target_id, part).unlink(missing_ok=True)
        for dataset in DATA:
            source, target = self.path(dataset, run_id, part), self.path(dataset, target_id, part)
            target.unlink(missing_ok=True)
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        _write_atomic(self.path(PART, target_id, part), _row(meta, found["summary"], {"counts": found["counts"]}),
                      row=True)

    # ------------------------------------------------------------- reading

    def _ids(self, base: Path, key: str) -> list[str]:
        if not base.is_dir():
            return []
        return sorted(
            p.name.split("=", 1)[1] for p in base.glob(f"{key}=*") if p.is_dir() and (p / FILE).is_file()
        )

    def run_ids(self) -> list[str]:
        """Every finished run, by id; a run set aside by a merge (`<id>.merged`) is not one."""
        return [r for r in self._ids(self.dataset_dir(RUN), "run_id") if "." not in r]

    def all_run_ids(self) -> list[str]:
        """Every run with a row or a finished part, finished or not."""
        ids = set(self._ids(self.dataset_dir(RUN), "run_id"))
        base = self.dataset_dir(PART)
        if base.is_dir():
            ids |= {p.name.split("=", 1)[1] for p in base.glob("run_id=*") if self._ids(p, "part")}
        return sorted(r for r in ids if "." not in r)

    def grouping(self, run_id: str, name: str) -> dict:
        """A finished run's trades grouped by one of `ledger.GROUPINGS`, as its row holds them."""
        if name not in GROUPINGS:
            raise ValueError(f"No grouping {name!r}; there are {', '.join(GROUPINGS)}")
        value = _read(self.path(RUN, run_id), [f"by_{name}"]).column(0)[0].as_py()
        return json.loads(value)

    def is_complete(self, run_id: str) -> bool:
        return self.path(RUN, run_id).is_file()

    def read_run(self, run_id: str) -> dict:
        """`{"meta", "summary"}` of a finished run."""
        return _unrow(_read(self.path(RUN, run_id)))

    def parts(self, run_id: str) -> list[str]:
        """The run's finished parts, by name, in order."""
        return self._ids(self.run_dir(PART, run_id), "part")

    def has_part(self, run_id: str, part: str) -> bool:
        return self.path(PART, run_id, part).is_file()

    def read_part(self, run_id: str, part: str) -> dict:
        """`{"meta", "summary", "counts"}` of a finished part."""
        return _unrow(_read(self.path(PART, run_id, part)))

    def _part_table(self, dataset: str, run_id: str, part: str, counts: dict) -> pa.Table | None:
        path = self.path(dataset, run_id, part)
        expected = counts.get(dataset, 0)
        if not expected:
            return None
        if not path.is_file():
            raise IncompletePart(f"{self.env} run {run_id} part {part} has no {dataset} file; it held {expected} rows")
        table = _read(path)
        if table.num_rows != expected:
            raise IncompletePart(f"{path} holds {table.num_rows} rows, not the {expected} its part was written with")
        return table

    def _counts(self, run_id: str, part: str) -> dict:
        """How many rows each of a finished part's files was written with: its row's `counts` alone."""
        value = _read(self.path(PART, run_id, part), ["counts"]).column(0)[0].as_py()
        return json.loads(value) if value else {}

    def read_part_trades(self, run_id: str, part: str) -> list[Trade]:
        table = self._part_table(TRADE, run_id, part, self._counts(run_id, part))
        return [] if table is None else trades_from(table)

    def trades(self, run_id: str) -> list[Trade]:
        """Every part's trades, in close order, as one replay reports them.

        One read of all the parts' files rather than one per part: a weekly run
        of seventy weeks is seventy files, and a reader of a sweep reads every run's.
        """
        parts = self.parts(run_id)
        if not parts:
            return []
        rows = ds.dataset([str(self.path(PART, run_id, p)) for p in parts], format="parquet",
                          schema=pa.schema([("counts", pa.string())])).to_table().column(0).to_pylist()
        expected = {}
        for part, counts in zip(parts, rows):
            n = json.loads(counts).get(TRADE, 0) if counts else 0
            if n:
                path = self.path(TRADE, run_id, part)
                if not path.is_file():
                    raise IncompletePart(f"{self.env} run {run_id} part {part} has no {TRADE} file; it held {n} rows")
                expected[str(path)] = n
        if not expected:
            return []
        dataset = ds.dataset(list(expected), format="parquet", schema=_TRADE_SCHEMA)
        for fragment in dataset.get_fragments():
            if fragment.metadata.num_rows != expected[fragment.path]:
                raise IncompletePart(f"{fragment.path} holds {fragment.metadata.num_rows} rows, "
                                     f"not the {expected[fragment.path]} its part was written with")
        trades = trades_from(dataset.to_table())
        trades.sort(key=lambda t: t.close_ts)
        return trades

    def records(self, dataset: str, run_id: str) -> pa.Table | None:
        """One record dataset of a run, every part, with a `part` column; None if it has no rows."""
        tables = []
        for part in self.parts(run_id):
            table = self._part_table(dataset, run_id, part, self._counts(run_id, part))
            if table is not None:
                tables.append(table.append_column("part", pa.array([part] * table.num_rows, pa.string())))
        if not tables:
            return None
        return pa.concat_tables(tables, promote_options="permissive")
