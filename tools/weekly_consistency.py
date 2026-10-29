"""Check, on real data, that a weekly run extended later is the run made all at once.

    python tools/weekly_consistency.py --env NAME --from 2026-09-07 --split 2026-09-21 --to 2026-10-04

- **A** runs `--weekly` from `--from` to the day before `--split`, then again
  from `--split` to `--to`: a backtest extended by a later run.
- **B** runs `--weekly` from `--from` to `--to` in one go.

Then every run of A is compared with B's, point to point: every trade of
every week, field by field; every figure of every summary, the run's and each
week's; every record file, byte for byte; and every meta but for when and in
which batch a week ran. Each is reported where it differs, and the exit status
is 0 only if nothing does. `tests/test_weekly.py` holds the same property on
synthetic data; this is the same check against the strategy and the data that
matter.

The env is never run in place. Its definition -- every file but its log and its
lock -- is copied into two fresh environments under `--out`, A and B, and each
writes its results to a lake of its own there, `A/lake` and `B/lake`: a copy
has the env's name, so its run ids are the env's, and in the data lake it
would write over the env's results.

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

from enoshima import lake
from enoshima.cli import quiet_strategy_logs
from enoshima.lake import Store
from enoshima.ledger import Trade
from enoshima.runner import Runner
from enoshima.settings import load_settings

# What a run leaves in its env, rather than what defines it -- and the folders it once wrote results to.
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


def run(env: Env, dt_range: DtRange, args, lake_root: Path) -> tuple[float, int]:
    """One `enoshima run --weekly`, its results in `lake_root`. Its wall time, in seconds, and its batch id."""
    env.enter()
    try:
        settings = load_settings(env.work_dir)
        runner = Runner(
            env,
            dt_range,
            optimize=args.optimize,
            chunk_days=args.chunk_days or settings.chunk_days,
            max_concurrency=args.max_concurrency or settings.max_concurrency,
            weekly=settings.weekly,
            fee_rates=settings.fee_rates,
            lake_root=lake_root,
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


def compare_row(report: Report, where: str, a: dict, b: dict) -> None:
    """Two runs' or parts' meta, but for when and in which batch, and their summaries."""
    meta_a = {k: v for k, v in a["meta"].items() if k not in RUN_FIELDS}
    meta_b = {k: v for k, v in b["meta"].items() if k not in RUN_FIELDS}
    compare_values(report, f"{where} meta", meta_a, meta_b)
    compare_values(report, f"{where} summary", a["summary"], b["summary"])


def compare_run(report: Report, a: Store, b: Store, run_id: str) -> None:
    if a.is_complete(run_id) != b.is_complete(run_id):
        report.differ(run_id, f"finished only in {'A' if a.is_complete(run_id) else 'B'}")
    elif a.is_complete(run_id):
        compare_row(report, run_id, a.read_run(run_id), b.read_run(run_id))
    parts_a, parts_b = set(a.parts(run_id)), set(b.parts(run_id))
    for part in sorted(parts_a ^ parts_b):
        report.differ(run_id, f"{part} only in {'A' if part in parts_a else 'B'}")
    for part in sorted(parts_a & parts_b):
        where = f"{run_id}/{part}"
        compare_row(report, where, a.read_part(run_id, part), b.read_part(run_id, part))
        compare_trades(report, where, a.read_part_trades(run_id, part), b.read_part_trades(run_id, part))
        for dataset in lake.DATA:
            fa, fb = a.path(dataset, run_id, part), b.path(dataset, run_id, part)
            if fa.is_file() != fb.is_file():
                report.differ(where, f"{dataset} only in {'A' if fa.is_file() else 'B'}")
            elif fa.is_file() and fa.read_bytes() != fb.read_bytes():
                report.differ(where, f"{dataset}: the files are not byte-identical")


def print_weeks(a: Store, b: Store, run_id: str, batches: tuple[int, ...]) -> None:
    """Each week's trades and pnl in A and B, and which of A's runs computed it."""
    print(f"  {'week':<10} {'run':>3} {'trades':>7} {'pnl5 A':>14} {'pnl5 B':>14}  range")
    for week in a.parts(run_id):
        found = a.read_part(run_id, week)
        sa, meta = found["summary"], found["meta"]
        sb = b.read_part(run_id, week)["summary"] if b.has_part(run_id, week) else {}
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
    store_a, store_b = Store(out / "A" / "lake", args.env), Store(out / "B" / "lake", args.env)
    a1, batch1 = run(env_a, first, args, store_a.root)
    a2, batch2 = run(env_a, second, args, store_a.root)
    print(f"B: {whole.start_str[:10]}..{whole.end_str[:10]}")
    b, _ = run(env_b, whole, args, store_b.root)
    print(f"Wall time: A {a1:.0f}s + {a2:.0f}s = {a1 + a2:.0f}s, B {b:.0f}s")

    ids_a, ids_b = set(store_a.all_run_ids()), set(store_b.all_run_ids())
    if not ids_a and not ids_b:
        print("Nothing ran: no week of the range has its data, and its overlap's, on disk")
        return 1

    report = Report()
    for run_id in sorted(ids_a ^ ids_b):
        report.differ(run_id, f"run only in {'A' if run_id in ids_a else 'B'}")
    for run_id in sorted(ids_a & ids_b):
        meta = store_a.read_part(run_id, store_a.parts(run_id)[0])["meta"]
        print(f"\n{run_id} {meta.get('param_set_id')}")
        print_weeks(store_a, store_b, run_id, (batch1, batch2))
        compare_run(report, store_a, store_b, run_id)

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
