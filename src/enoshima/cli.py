"""The `enoshima` command.

Run as: `enoshima <command>`, or from a checkout
`PYTHONPATH=src python -m enoshima.cli <command>`.

```
enoshima run     --env NAME --from 2026-06-01 --to 2026-06-10
enoshima results --env NAME
enoshima clean   --env NAME
```

`run` is the one that matters; the rest exist so that what a run produced can be
found and read without reading the source.

Everything a run needs to know beyond the dates is somewhere already, so it is not
a flag here:

- **The data** is wherever komachi says it is. `komachi local` lists it, and
  `ROOT_PATH=... enoshima run ...` points one run elsewhere, by komachi's own rule.
- **The output** is the environment directory: `result/`, `run_log/`,
  `best_result/` and `env.log` beside the definition they came from.
- **The strategy and variant** are `trading.json`'s.
- **Recording to InfluxDB**, and how a run is sized, are `enoshima.json`'s -- see
  `enoshima.settings`. `--chunk-days` and `--max-concurrency` still override the
  file for one run.

Nothing is written to the terminal. Every command enters its environment, and
`qate_env.Env` sends the root logger to `env.log` there -- Enoshima's own
logging, the strategy's, and the libraries' -- so the record of a run sits next
to the run. Enoshima configures no handler of its own. `tail -f env.log` in the
environment directory is how a run is watched.
"""

import argparse
import json
import shutil
import sys
from logging import getLogger
from pathlib import Path

from qate.util.dt_range import DtRange
from qate_env import sys_env
from qate_env.env import Env
from qate_env.env_name import EnvName

from enoshima.runner import Runner
from enoshima.settings import SETTINGS_FILE, InfluxSettings, load_settings

LOG = getLogger(__name__)


def load_env(env_name: EnvName) -> Env:
    """The environment, not yet read. `load_trading_env` reads the files it needs."""
    return Env(sys_env.get_env_root_dir(), env_name)


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


def influx_store(settings: InfluxSettings, work_dir: str):
    """The InfluxDB a run records to, checked before any data is replayed.

    A server that is down is found out now rather than after a long replay, and
    the run is refused rather than silently left unrecorded: the environment asked
    for it.
    """
    try:
        from enoshima.influx import InfluxdbStore
    except ImportError:
        LOG.error(
            f"{SETTINGS_FILE} in {work_dir} asks for InfluxDB, which needs the influx extra: "
            "pip install 'kamakuraquantlab-enoshima[influx]'"
        )
        return None

    store = InfluxdbStore(sys_env.get_influxdb(settings.keys), org=settings.org)
    if not store.ping():
        LOG.error(
            f"InfluxDB is not answering at {store._write_url}. Start it, or remove "
            f'"influx" from {SETTINGS_FILE} in {work_dir}.'
        )
        return None
    try:
        store.org
    except Exception as e:
        LOG.error(f'{e}. Set "org" under "influx" in {SETTINGS_FILE} in {work_dir}.')
        return None
    return store


# ------------------------------------------------------------------ subcommands


def cmd_run(args) -> int:
    launch_dir = add_launch_dir_to_path()
    env = load_env(args.env)
    dt_range = DtRange.from_strings(args.from_dt, args.to_dt)

    # chdir into the environment, log there, and lock it, so two runs cannot
    # interleave their results. First, so that everything after is in env.log.
    env.enter()
    try:
        settings = load_settings(env.work_dir)
        influx = None
        if settings.influx is not None:
            influx = influx_store(settings.influx, env.work_dir)
            if influx is None:
                return 1

        LOG.info(f"Loading strategies from {launch_dir}")
        if influx is not None:
            LOG.info(f"Recording to InfluxDB, organisation {influx.org}")
        runner = Runner(
            env,
            dt_range,
            optimize=args.optimize,
            chunk_days=args.chunk_days or settings.chunk_days,
            max_concurrency=args.max_concurrency or settings.max_concurrency,
            force=args.force,
            influx=influx,
        )
        runner.run()
        if args.optimize:
            runner.post_analysis()
    except Exception:
        LOG.exception("Run failed")
        return 1
    finally:
        env.leave()
    return 0


def cmd_results(args) -> int:
    env = load_env(args.env)
    env.visit()
    run_log_dir = Path(env.work_dir) / "run_log"
    if not run_log_dir.is_dir():
        LOG.info(f"No runs recorded for {args.env}")
        return 1

    rows = []
    for path in run_log_dir.glob("*.json"):
        try:
            rows.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    if not rows:
        LOG.info(f"No runs recorded for {args.env}")
        return 1

    # Best first, on the middle slippage assumption -- see Runner.post_analysis.
    rows.sort(key=lambda r: r.get("result", {}).get("pnl5") or float("-inf"), reverse=True)
    LOG.info(f"{'pnl5':>12s} {'trades':>7s} {'sharpe':>8s}  {'range':23s} params")
    for row in rows[: args.limit]:
        result = row.get("result", {})
        pnl = result.get("pnl5")
        sharpe = result.get("sharpe_ratio_annualized0")
        params = " ".join(f"{k}={v}" for k, v in (row.get("params") or {}).items())
        date_range = f"{row.get('start_str', '?')}..{row.get('end_str', '?')}"
        LOG.info(
            f"{'n/a' if pnl is None else f'{pnl:+12.2f}':>12s} "
            f"{result.get('pnl_cnt0', 0):7d} "
            f"{'n/a' if sharpe is None else f'{sharpe:8.3f}':>8s}  "
            f"{date_range:23s} {params}"
        )
    return 0


def cmd_clean(args) -> int:
    env = load_env(args.env)
    env.visit()
    out = Path(env.work_dir)
    # `result/` is what versions that wrote parquet left behind.
    targets = [out / "result", out / "run_log", out / "best_result"]
    existing = [t for t in targets if t.exists()]
    if not existing:
        LOG.info(f"Nothing to remove under {out}")
        return 0

    LOG.info("This will permanently delete:")
    for t in existing:
        LOG.info(f"  {t}")
    if not args.yes:
        # Results are the whole product of a long run. Never removed without a
        # yes, and never on a schedule.
        LOG.info("Re-run with --yes to confirm.")
        return 1
    for t in existing:
        shutil.rmtree(t)
        LOG.info(f"Removed {t}")
    return 0


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enoshima", description="Backtest a strategy against recorded market data.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a backtest")
    run.add_argument("--env", required=True, type=EnvName, help="environment name, e.g. EXAMPLE_BACKTEST")
    run.add_argument("--from", dest="from_dt", required=True, help="start date: 20260601, 2026-06-01, or 20260601_0900")
    run.add_argument("--to", dest="to_dt", default="", help="end date; defaults to the end of --from's day")
    run.add_argument(
        "--optimize",
        choices=["grid", "combo"],
        default=None,
        help="grid: every combination in param_grid.json; combo: the explicit list in params_combo.json",
    )
    run.add_argument(
        "--chunk-days", type=int, default=None, help=f"days loaded at once (memory); overrides {SETTINGS_FILE}"
    )
    run.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help=f"parameter sets per data pass; overrides {SETTINGS_FILE}",
    )
    run.add_argument("--force", action="store_true", help="re-run parameter sets that already have a result")
    run.set_defaults(func=cmd_run)

    results = sub.add_parser("results", help="list recorded runs, best first")
    results.add_argument("--env", required=True, type=EnvName)
    results.add_argument("--limit", type=int, default=25)
    results.set_defaults(func=cmd_results)

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
