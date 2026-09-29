"""The `enoshima` command.

```
enoshima run     --env NAME --from 2026-06-01 --to 2026-06-10
enoshima markets
enoshima results --env NAME
enoshima export  --env NAME
enoshima clean   --env NAME
```

`run` is the one that matters; the rest exist so that what a run produced can be
found, read and charted without reading the source.

Logging is split three ways on purpose: progress to stdout, errors to stderr, and
everything including the strategy's own logging to `env.log` inside the
environment directory. A backtest is long-running and mostly silent, so the
terminal should carry what is happening and nothing else, while the full record
stays on disk next to the results.
"""

import argparse
import logging
import sys
from pathlib import Path

from qate.boot.config import TradingProfile
from qate.env import sys_env
from qate.env.env import Env
from qate.env.env_name import EnvName
from qate.util.dt_range import DtRange

from .bronze import BronzeStore, data_root
from .data_source import DEFAULT_CHUNK_DAYS
from .runner import MAX_CONCURRENCY, Runner

LOG = logging.getLogger("enoshima.cli")


class _EnoshimaOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("enoshima")


def setup_terminal_logging() -> None:
    """Progress on stdout, errors on stderr. The file handler is env.enter()'s."""
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(logging.INFO)
    stdout.setFormatter(logging.Formatter("[{asctime}] {message}", style="{", datefmt="%H:%M:%S"))
    stdout.addFilter(_EnoshimaOnly())

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.ERROR)
    stderr.setFormatter(
        logging.Formatter("[{asctime}] {levelname} {name} - {message}", style="{", datefmt="%H:%M:%S")
    )

    root = logging.getLogger()
    root.addHandler(stdout)
    root.addHandler(stderr)


def basic_logging() -> None:
    """For subcommands that do not enter an environment."""
    logging.basicConfig(
        level=logging.INFO,
        format="[{asctime}] {message}",
        style="{",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def load_env(env_name: EnvName) -> Env:
    return Env.load_env(sys_env.get_env_root_dir(), env_name)


def add_launch_dir_to_path() -> str:
    """Put the directory the command was launched from on `sys.path`.

    A strategy is loaded by module name — `trading.json` says
    `strategy_module_name: "my_strategy"` and the loader imports it — so the
    directory holding it has to be importable. A console script does not get the
    working directory on `sys.path` the way `python -m` does, so without this a
    user's own strategy is never found and the error is a bare
    `ModuleNotFoundError` naming their module.

    Called before `env.enter()`, which chdirs into the environment directory:
    afterwards the launch directory is no longer the working directory and this
    would record the wrong path.
    """
    launch_dir = str(Path.cwd())
    if launch_dir not in sys.path:
        sys.path.insert(0, launch_dir)
    return launch_dir


# ------------------------------------------------------------------ subcommands


def cmd_run(args) -> int:
    launch_dir = add_launch_dir_to_path()
    env = load_env(args.env)
    if args.variant:
        profile: TradingProfile = env.get("trading")
        profile.variant = args.variant

    dt_range = DtRange.from_strings(args.from_dt, args.to_dt)

    try:
        # Takes the environment's lock and chdirs into it, so two runs of one
        # environment cannot interleave their results.
        env.enter()
        setup_terminal_logging()
        LOG.info(f"Loading strategies from {launch_dir}")
        runner = Runner(
            env,
            dt_range,
            optimize=args.optimize,
            data_root=args.data_root,
            chunk_days=args.chunk_days,
            max_concurrency=args.max_concurrency,
            force=args.force,
        )
        runner.run()
        if args.optimize:
            runner.post_analysis()
    finally:
        env.leave()
    return 0


def cmd_markets(args) -> int:
    basic_logging()
    root = data_root(args.data_root)
    store = BronzeStore(root)
    markets = store.markets()
    print(f"Data root: {root}")
    if not markets:
        print("Nothing on disk. Download some with: komachi get --help")
        return 1
    print(f"{'market':28s} {'dataset':11s} {'days':>5s}  range")
    for market in markets:
        for dataset in ("Trade", "OrderBook"):
            dates = store.available_dates(market, dataset)
            if not dates:
                continue
            print(f"{market.id:28s} {dataset:11s} {len(dates):5d}  {dates[0]} .. {dates[-1]}")
    return 0


def cmd_results(args) -> int:
    import json

    basic_logging()
    env = load_env(args.env)
    run_log_dir = Path(env.work_dir) / "run_log"
    if not run_log_dir.is_dir():
        print(f"No runs recorded for {args.env}")
        return 1

    rows = []
    for path in run_log_dir.glob("*.json"):
        try:
            rows.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    if not rows:
        print(f"No runs recorded for {args.env}")
        return 1

    # Best first, on the middle slippage assumption -- see Runner.post_analysis.
    rows.sort(key=lambda r: r.get("result", {}).get("pnl5") or float("-inf"), reverse=True)
    print(f"{'pnl5':>12s} {'trades':>7s} {'sharpe':>8s}  {'range':23s} params")
    for row in rows[: args.limit]:
        result = row.get("result", {})
        pnl = result.get("pnl5")
        sharpe = result.get("sharpe_ratio_annualized0")
        params = " ".join(f"{k}={v}" for k, v in (row.get("params") or {}).items())
        date_range = f"{row.get('start_str', '?')}..{row.get('end_str', '?')}"
        print(
            f"{'n/a' if pnl is None else f'{pnl:+12.2f}':>12s} "
            f"{result.get('pnl_cnt0', 0):7d} "
            f"{'n/a' if sharpe is None else f'{sharpe:8.3f}':>8s}  "
            f"{date_range:23s} {params}"
        )
    return 0


def cmd_export(args) -> int:
    basic_logging()
    try:
        from .influx import InfluxdbStore
    except ImportError:
        print(
            "Exporting needs the influx extra: pip install 'kamakuraquantlab-enoshima[influx]'",
            file=sys.stderr,
        )
        return 1

    from .influx import export_env

    env = load_env(args.env)
    store = InfluxdbStore(sys_env.get_influxdb(args.keys), org=args.org)
    written = export_env(env.work_dir, store, str(env.name), args.batch_id)
    print(f"Exported {written} metric objects")
    return 0


def cmd_clean(args) -> int:
    import shutil

    basic_logging()
    env = load_env(args.env)
    targets = [Path(env.work_dir) / "result", Path(env.work_dir) / "run_log", Path(env.work_dir) / "best_result"]
    existing = [t for t in targets if t.exists()]
    if not existing:
        print(f"Nothing to remove under {env.work_dir}")
        return 0

    print("This will permanently delete:")
    for t in existing:
        print(f"  {t}")
    if not args.yes:
        # Results are the whole product of a long run. Never removed without a
        # yes, and never on a schedule.
        print("Re-run with --yes to confirm.")
        return 1
    for t in existing:
        shutil.rmtree(t)
        print(f"Removed {t}")
    return 0


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enoshima", description="Backtest a strategy against recorded market data.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a backtest")
    run.add_argument("--env", required=True, type=EnvName, help="environment name, e.g. EXAMPLE_BACKTEST")
    run.add_argument("--from", dest="from_dt", required=True, help="start date: 20260601, 2026-06-01, or 20260601_0900")
    run.add_argument("--to", dest="to_dt", default="", help="end date; defaults to the end of --from's day")
    run.add_argument("--variant", default="", help="override the variant in trading.json")
    run.add_argument(
        "--optimize",
        choices=["grid", "combo"],
        default=None,
        help="grid: every combination in param_grid.json; combo: the explicit list in params_combo.json",
    )
    run.add_argument("--data-root", default=None, help="bronze data root; defaults to komachi's")
    run.add_argument("--chunk-days", type=int, default=DEFAULT_CHUNK_DAYS, help="days loaded at once (memory)")
    run.add_argument("--max-concurrency", type=int, default=MAX_CONCURRENCY, help="parameter sets per data pass")
    run.add_argument("--force", action="store_true", help="re-run parameter sets that already have a result")
    run.set_defaults(func=cmd_run)

    markets = sub.add_parser("markets", help="what market data is on disk")
    markets.add_argument("--data-root", default=None)
    markets.set_defaults(func=cmd_markets)

    results = sub.add_parser("results", help="list recorded runs, best first")
    results.add_argument("--env", required=True, type=EnvName)
    results.add_argument("--limit", type=int, default=25)
    results.set_defaults(func=cmd_results)

    export = sub.add_parser("export", help="load a run's metrics into InfluxDB")
    export.add_argument("--env", required=True, type=EnvName)
    export.add_argument("--batch-id", type=int, default=None, help="one batch; defaults to every batch")
    export.add_argument("--keys", default="DEFAULT", help="section in ~/.qate/influxdb.keys")
    export.add_argument("--org", default="qate", help="InfluxDB organisation")
    export.set_defaults(func=cmd_export)

    clean = sub.add_parser("clean", help="delete an environment's results and run logs")
    clean.add_argument("--env", required=True, type=EnvName)
    clean.add_argument("--yes", action="store_true", help="required; results are not deleted without it")
    clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
