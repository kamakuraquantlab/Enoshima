"""Bringing an environment's results from before the lake into it: `enoshima import-run-log`.

Results used to be written into the environment directory:

    run_log/<run_id>/meta.json, summary.json, trades.csv, by_*.json
    run_log/<run_id>/weeks/<YYYY-Www>/meta.json, summary.json, trades.csv

A run now writes to the lake (`lake`), and every reader -- `results`,
`enoshima web` -- reads from there, so a folder not imported is not seen. Importing replays nothing: each week, or a temporary
run's root, becomes a part with its meta, summary and trades, and each run
with a `summary.json` its `BacktestRun` row. The run ids are the folders'
names, the same hash a run computes today, so a weekly run carries on from the
imported weeks. The orders, PnL updates and bars of those runs were only ever
in InfluxDB, and the imported parts have none.

Enoshima chooses nothing, so the `best_result/` and `post_analysis/` folders
an earlier version wrote beside `run_log/` are not its to import; they are
left where they are.

A run already in the lake is left alone: what is there was written by a
replay, and is at least as new. The folders are not removed -- Enoshima never
deletes results; delete `run_log/` once the import looks right. `apply=False`, the default, only reports.
"""

import csv
import json
from logging import getLogger
from pathlib import Path

from .lake import WHOLE, Store
from .ledger import Trade

LOG = getLogger(__name__)

RUN_LOG_DIR = "run_log"
WEEKS_DIR = "weeks"


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def read_trades(path: Path) -> list[Trade]:
    """A folder's `trades.csv`, as it was written."""
    with open(path / "trades.csv", newline="") as f:
        return [Trade(**{k: _cell(k, v) for k, v in row.items()}) for row in csv.DictReader(f)]


def _cell(column: str, value: str):
    if column in ("exchange", "symbol", "side"):
        return value
    return None if value == "" else float(value)


def _complete(path: Path) -> bool:
    return (path / "meta.json").is_file() and (path / "summary.json").is_file()


def import_env(work_dir: Path, store: Store, *, apply: bool = False) -> dict:
    """Import `work_dir`'s run folders into `store`. Returns what was (or would be) imported."""
    report = {"runs": 0, "parts": 0, "skipped": 0}
    base = Path(work_dir) / RUN_LOG_DIR
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        # `.tmp-*` was being written, `.old-*` replaced, `.merged` set aside by a merge.
        if not folder.is_dir() or "." in folder.name:
            continue
        run_id = folder.name
        if store.is_complete(run_id) or store.parts(run_id):
            report["skipped"] += 1
            continue
        weeks = folder / WEEKS_DIR
        parts = ([(w.name, w) for w in sorted(weeks.iterdir()) if w.is_dir() and "." not in w.name and _complete(w)]
                 if weeks.is_dir() else [(WHOLE, folder)] if _complete(folder) else [])
        if not parts:
            continue
        report["runs"] += 1
        report["parts"] += len(parts)
        if not apply:
            continue
        trades = []
        for part, path in parts:
            trades.extend(part_trades := read_trades(path))
            store.write_part(run_id, part, _json(path / "meta.json"), _json(path / "summary.json"), part_trades)
        if _complete(folder):
            trades.sort(key=lambda t: t.close_ts)
            store.write_run(run_id, _json(folder / "meta.json"), _json(folder / "summary.json"), trades)
    LOG.info(
        f"import-run-log {store.env}: {report['runs']} run(s), {report['parts']} part(s); "
        f"{report['skipped']} already in the lake"
        f"{'' if apply else ' (dry run)'}"
    )
    return report
