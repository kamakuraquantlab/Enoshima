"""A run's folder: `run_log/<run_id>/`.

    meta.json          what was run: env, strategy, config, params, range, bucket
    summary.json       the whole-range result, at each slippage rate
    trades.csv         one row per closed position (`ledger.Trade`)
    by_<grouping>.json the trades aggregated by each of `ledger.GROUPINGS`

`run_id` is `BacktestLog.get_id`, the deterministic hash of what the run
computes, so a sweep that dies part-way resumes: a folder with a
`summary.json` is a finished run, and is skipped unless `--force`.

A folder is written whole or not at all. It is built under a temporary name
beside its final one and renamed into place, so a run killed while writing
leaves a `.tmp` folder that nothing reads, never a finished-looking folder
missing its trades. `--force` replaces the old folder the same way.

PnL is the trading data kept here rather than in InfluxDB, because it is what a
result is evaluated from; see `result`.

A weekly run (`weekly`) has the same files at its root, and under
`weeks/<YYYY-Www>/` each week's `meta.json`, `summary.json` and `trades.csv`,
each week written whole the same way. The weeks are what a run writes; the
root is rebuilt from them (`write_cumulative`), so it reads as an ordinary run
over its weeks' range.
"""

import csv
import json
import os
import shutil
from dataclasses import astuple
from pathlib import Path

from qate.util.encoder import Encoder

from .ledger import GROUPINGS, TRADE_COLUMNS, Trade, aggregate

RUN_LOG_DIR = "run_log"
META = "meta.json"
SUMMARY = "summary.json"
TRADES = "trades.csv"
WEEKS_DIR = "weeks"


def grouping_file(name: str) -> str:
    return f"by_{name}.json"


def run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / RUN_LOG_DIR / run_id


def week_dir(root: Path, run_id: str, week_key: str) -> Path:
    return run_dir(root, run_id) / WEEKS_DIR / week_key


def is_complete(path: Path) -> bool:
    return (path / SUMMARY).is_file()


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=False, cls=Encoder))


def write(root: Path, run_id: str, meta: dict, summary: dict, trades: list[Trade]) -> Path:
    """Write a run's folder, replacing any earlier one of the same id."""
    return _write_whole(run_dir(root, run_id), meta, summary, trades, GROUPINGS)


def write_week(root: Path, run_id: str, week_key: str, meta: dict, summary: dict, trades: list[Trade]) -> Path:
    """Write one week of a weekly run, replacing any earlier one of the same week.

    No `by_*.json`: a window's groupings are computed from its weeks' trades together.
    """
    return _write_whole(week_dir(root, run_id, week_key), meta, summary, trades, ())


def write_cumulative(root: Path, run_id: str, meta: dict, summary: dict, trades: list[Trade]) -> Path:
    """A weekly run's root files, rebuilt in place around its `weeks/`.

    The folder cannot be renamed into place, as it holds the weeks, so each file
    is. `summary.json` is removed first and written last: in between the folder
    is not complete, rather than complete with a summary of other trades.
    """
    final = run_dir(root, run_id)
    final.mkdir(parents=True, exist_ok=True)
    (final / SUMMARY).unlink(missing_ok=True)
    _replace(final / META, lambda f: _dump(f, meta))
    _replace(final / TRADES, lambda f: _write_trades(f, trades))
    for name in GROUPINGS:
        _replace(final / grouping_file(name), lambda f, name=name: _dump(f, aggregate(trades, name)))
    _replace(final / SUMMARY, lambda f: _dump(f, summary))
    return final


def remove_summary(root: Path, run_id: str) -> None:
    """Mark a weekly run's root not complete. Its weeks are kept; the root is rebuilt from them."""
    (run_dir(root, run_id) / SUMMARY).unlink(missing_ok=True)


def _replace(path: Path, write) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    write(tmp)
    tmp.replace(path)


def _write_trades(path: Path, trades: list[Trade]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(TRADE_COLUMNS)
        writer.writerows(astuple(t) for t in trades)


def _write_whole(final: Path, meta: dict, summary: dict, trades: list[Trade], groupings) -> Path:
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"{final.name}.tmp-{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()

    _dump(tmp / META, meta)
    _write_trades(tmp / TRADES, trades)
    for name in groupings:
        _dump(tmp / grouping_file(name), aggregate(trades, name))
    _dump(tmp / SUMMARY, summary)  # last: its presence is what "complete" means

    old = None
    if final.exists():
        old = final.with_name(f"{final.name}.old-{os.getpid()}")
        final.rename(old)
    tmp.rename(final)
    if old is not None:
        shutil.rmtree(old)
    return final


def read_json(path: Path, name: str) -> dict:
    return json.loads((path / name).read_text())


def read_trades(path: Path) -> list[Trade]:
    """A run folder's `trades.csv`, as written."""
    with open(path / TRADES, newline="") as f:
        rows = csv.DictReader(f)
        return [Trade(**{k: _cell(k, v) for k, v in row.items()}) for row in rows]


def _cell(column: str, value: str):
    if column in ("exchange", "symbol", "side"):
        return value
    return None if value == "" else float(value)


def iter_complete(root: Path):
    """Every finished run folder under `root`'s `run_log/`."""
    base = Path(root) / RUN_LOG_DIR
    if not base.is_dir():
        return
    for path in sorted(base.iterdir()):
        if path.is_dir() and "." not in path.name and is_complete(path):
            yield path
