"""Check, on real data, that a weekly run extended later is the run made all at once.

    python tools/weekly_consistency.py --env NAME --from 2026-09-07 --split 2026-09-21 --to 2026-10-04

- **A** runs `--weekly` from `--from` to the day before `--split`, then again
  from `--split` to `--to`: a backtest extended by a later run.
- **B** runs `--weekly` from `--from` to `--to` in one go.

Then every run folder of A is compared with B's, point to point: every trade of
every week and of the root, field by field; every figure of every
`summary.json` and `by_*.json`; and every `meta.json` but for when and in
which batch a week ran. Each is reported where it differs, and the exit status
is 0 only if nothing does. `tests/test_weekly.py` holds the same property on
synthetic data; this is the same check against the strategy and the data that
matter.

The env is never run in place. Its definition -- every file but its results,
its log and its lock -- is copied into two fresh environments under `--out`, A
and B, and each writes there. InfluxDB is off: a weekly run's bucket is named
by its run id, which a copy shares with the env it came from, and writing one
week clears that week's points from the bucket.

The data is komachi's, as for any run: `ROOT_PATH` in komachi's settings file.
The strategy must be importable, as for `enoshima run`. `enoshima.json` is the
env's, so the weekly preload and overlap, `chunk_days` and `max_concurrency`
are what the env runs with. Logs go to each copy's `env.log`; the report is
printed.

This is a tool, not part of the package: `tools/` is not in the wheel.
"""

import argparse
import datetime as dt
import math
import shutil
import sys
import tempfile
import time
from dataclasses import fields
from pathlib import Path

from qate.util.dt_range import DtRange
from qate_env import sys_env
from qate_env.env import Env

from enoshima import run_dir
from enoshima.cli import quiet_strategy_logs
from enoshima.ledger import Trade
from enoshima.runner import Runner
from enoshima.settings import load_settings

# What a run leaves in its env, rather than what defines it.
OUTPUTS = ("run_log", "best_result", "post_analysis", "env.log*", "*.lock", "lock")
# What differs between two runs that computed the same numbers: when, and in which batch.
RUN_FIELDS = {"batch_id", "start_ts", "end_ts", "execution_time"}
MAX_SHOWN = 20


def copy_env(name: str, root: Path) -> Env:
    source = Path(sys_env.get_env_root_dir()) / name
    if not source.is_dir():
        sys.exit(f"No env {name} in {source.parent}")
    shutil.copytree(source, root / name, ignore=shutil.ignore_patterns(*OUTPUTS))
    return Env(str(root), name)


def run(env: Env, dt_range: DtRange, args) -> tuple[float, int]:
    """One `enoshima run --weekly`, without InfluxDB. Its wall time, in seconds, and its batch id."""
    env.enter()
    try:
        settings = load_settings(env.work_dir)
        runner = Runner(
            env,
            dt_range,
            optimize=args.optimize,
            chunk_days=args.chunk_days or settings.chunk_days,
            max_concurrency=args.max_concurrency or settings.max_concurrency,
            influx=None,
            weekly=settings.weekly,
        )
        quiet_strategy_logs(runner.definition.module_name, settings.strategy_log_level)
        started = time.monotonic()
        runner.run()
        if runner.failed:
            sys.exit(f"{len(runner.failed)} backtest(s) failed in {env.work_dir}; see its env.log")
        return time.monotonic() - started, runner.batch_id
    finally:
        env.leave()


class Report:
    def __init__(self):
        self.differences = 0
        self.compared = 0

    def differ(self, where: str, what: str) -> None:
        self.differences += 1
        if self.differences <= MAX_SHOWN:
            print(f"  DIFF {where}: {what}")
        elif self.differences == MAX_SHOWN + 1:
            print("  ... (further differences counted, not shown)")


def same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def compare_values(report: Report, where: str, a, b, path: str = "") -> None:
    """Every leaf of two JSON values, exactly."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                report.differ(where, f"{path}{key} only in {'A' if key in a else 'B'}")
            else:
                compare_values(report, where, a[key], b[key], f"{path}{key}.")
        return
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            compare_values(report, where, x, y, f"{path}{i}.")
        return
    report.compared += 1
    if not same(a, b):
        delta = f" (B-A {b - a:+.6g})" if isinstance(a, (int, float)) and isinstance(b, (int, float)) else ""
        report.differ(where, f"{path.rstrip('.')}: A={a!r} B={b!r}{delta}")


def jst(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(hours=9))).strftime("%m-%d %H:%M:%S")


def compare_trades(report: Report, where: str, a: list[Trade], b: list[Trade]) -> None:
    if len(a) != len(b):
        report.differ(where, f"{len(a)} trades in A, {len(b)} in B")
    for i, (x, y) in enumerate(zip(a, b)):
        for f in fields(Trade):
            report.compared += 1
            if not same(getattr(x, f.name), getattr(y, f.name)):
                report.differ(
                    where,
                    f"trade {i} (opened {jst(x.open_ts)}, closed {jst(x.close_ts)}) "
                    f"{f.name}: A={getattr(x, f.name)!r} B={getattr(y, f.name)!r}",
                )


def compare_folder(report: Report, a: Path, b: Path) -> None:
    files_a = {str(f.relative_to(a)) for f in a.rglob("*") if f.is_file()}
    files_b = {str(f.relative_to(b)) for f in b.rglob("*") if f.is_file()}
    for name in sorted(files_a ^ files_b):
        report.differ(a.name, f"{name} only in {'A' if name in files_a else 'B'}")
    for name in sorted(files_a & files_b):
        where = f"{a.name}/{name}"
        fa, fb = a / name, b / name
        if fa.name == run_dir.TRADES:
            compare_trades(report, where, run_dir.read_trades(fa.parent), run_dir.read_trades(fb.parent))
            if fa.read_bytes() != fb.read_bytes():
                report.differ(where, "the files are not byte-identical")
        elif fa.suffix == ".json":
            va, vb = run_dir.read_json(fa.parent, fa.name), run_dir.read_json(fb.parent, fb.name)
            if fa.name == run_dir.META:
                va = {k: v for k, v in va.items() if k not in RUN_FIELDS}
                vb = {k: v for k, v in vb.items() if k not in RUN_FIELDS}
            compare_values(report, where, va, vb)
        elif fa.read_bytes() != fb.read_bytes():
            report.differ(where, "the files differ")


def print_weeks(a: Path, b: Path, batches: tuple[int, ...]) -> None:
    """Each week's trades and pnl in A and B, and which of A's runs computed it."""
    print(f"  {'week':<10} {'run':>3} {'trades':>7} {'pnl5 A':>14} {'pnl5 B':>14}  range")
    weeks = a / run_dir.WEEKS_DIR
    for week in sorted(p.name for p in weeks.iterdir()) if weeks.is_dir() else []:
        sa = run_dir.read_json(weeks / week, run_dir.SUMMARY)
        sb = run_dir.read_json(b / run_dir.WEEKS_DIR / week, run_dir.SUMMARY) \
            if (b / run_dir.WEEKS_DIR / week / run_dir.SUMMARY).is_file() else {}
        meta = run_dir.read_json(weeks / week, run_dir.META)
        which = batches.index(meta["batch_id"]) + 1 if meta["batch_id"] in batches else "?"
        part = " (partial)" if meta.get("partial") else ""
        print(f"  {week:<10} {which:>3} {sa['pnl_cnt0']:>7} {sa['pnl5']:>14.2f} {sb.get('pnl5', float('nan')):>14.2f}"
              f"  {meta['start_str'][:10]}..{meta['end_str'][:10]}{part}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", required=True, help="the env to copy; it is never written to")
    parser.add_argument("--from", dest="from_dt", required=True, help="first day, e.g. 2026-09-07")
    parser.add_argument("--split", required=True, help="the Monday A's second run starts on")
    parser.add_argument("--to", dest="to_dt", required=True, help="last day")
    parser.add_argument("--optimize", choices=["grid"], help="every parameter set of the grid, not the default")
    parser.add_argument("--chunk-days", type=int)
    parser.add_argument("--max-concurrency", type=int)
    parser.add_argument("--out", help="where the two copies go (default: a new temporary directory)")
    parser.add_argument("--keep", action="store_true", help="keep the copies when nothing differs")
    args = parser.parse_args(argv)

    split = dt.date.fromisoformat(args.split)
    if split.weekday() != 0:
        sys.exit(f"--split {split} is a {split:%A}; it must be a Monday, or A's runs would share its week")
    first = DtRange.from_strings(args.from_dt, (split - dt.timedelta(days=1)).isoformat())
    second = DtRange.from_strings(args.split, args.to_dt)
    whole = DtRange.from_strings(args.from_dt, args.to_dt)

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="weekly_consistency_"))
    out.mkdir(parents=True, exist_ok=True)
    env_a, env_b = copy_env(args.env, out / "A"), copy_env(args.env, out / "B")
    print(f"Copies of {args.env} in {out}")

    print(f"A: {first.start_str[:10]}..{first.end_str[:10]}, then {second.start_str[:10]}..{second.end_str[:10]}")
    a1, batch1 = run(env_a, first, args)
    a2, batch2 = run(env_a, second, args)
    print(f"B: {whole.start_str[:10]}..{whole.end_str[:10]}")
    b, _ = run(env_b, whole, args)
    print(f"Wall time: A {a1:.0f}s + {a2:.0f}s = {a1 + a2:.0f}s, B {b:.0f}s")

    root_a, root_b = Path(env_a.work_dir) / run_dir.RUN_LOG_DIR, Path(env_b.work_dir) / run_dir.RUN_LOG_DIR
    ids_a = {p.name for p in root_a.iterdir()} if root_a.is_dir() else set()
    ids_b = {p.name for p in root_b.iterdir()} if root_b.is_dir() else set()
    if not ids_a and not ids_b:
        print("Nothing ran: no week of the range has its data, and its overlap's, on disk")
        return 1

    report = Report()
    for run_id in sorted(ids_a ^ ids_b):
        report.differ(run_id, f"run folder only in {'A' if run_id in ids_a else 'B'}")
    for run_id in sorted(ids_a & ids_b):
        folder_a, folder_b = root_a / run_id, root_b / run_id
        meta = run_dir.read_json(folder_a, run_dir.META)
        print(f"\n{run_id} {meta.get('param_set_id')}")
        print_weeks(folder_a, folder_b, (batch1, batch2))
        compare_folder(report, folder_a, folder_b)

    print(f"\n{report.compared} values compared, {report.differences} different")
    if report.differences:
        print(f"A and B differ. The copies are kept in {out}")
        return 1
    print("A and B are identical")
    if not args.keep and not args.out:
        shutil.rmtree(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
