"""The `enoshima` command.

Run as: `enoshima <command>`, or from a checkout
`PYTHONPATH=src python -m enoshima.cli <command>`.

```
enoshima run     --env NAME --from 2026-06-01 --to 2026-06-10
enoshima results --env NAME
```

`run` is the one that matters; the rest exist so that what a run produced can be
found and read without reading the source.

Everything a run needs to know beyond the dates is somewhere already, so it is not
a flag here:

- **The data** is wherever komachi says it is: `ROOT_PATH` in komachi's settings
  file. `komachi local` lists it; to replay another tree, change it there.
- **The output** is the environment directory: `run_log/`, `best_result/` and
  `env.log` beside the definition they came from. To clear it, delete them.
- **The strategy and variant** are `trading.json`'s. The strategy module is
  imported by name, so it must be installed or on `PYTHONPATH`.
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
import logging
import sys
from logging import getLogger
from pathlib import Path

from qate.util.dt_range import DtRange
from qate_env import sys_env
from qate_env.env import Env
from qate_env.env_name import EnvName

from enoshima import run_dir
from enoshima.influx import InfluxdbStore
from enoshima.runner import Runner
from enoshima.settings import SETTINGS_FILE, load_settings

LOG = getLogger(__name__)


def quiet_strategy_logs(strategy_module_name: str, level: str) -> None:
    """Set the strategy's loggers to `level` for this run. Levels only: no handler is added.

    The strategy's package, and `qate.trading`, where `Strategy` logs every fill.
    Enoshima's own loggers, and qate's event loop, keep theirs.
    """
    for name in (strategy_module_name.split(".")[0], "qate.trading"):
        logging.getLogger(name).setLevel(level)


def cmd_run(args) -> int:
    env = Env(sys_env.get_env_root_dir(), args.env)
    dt_range = DtRange.from_strings(args.from_dt, args.to_dt)

    env.enter()
    try:
        settings = load_settings(env.work_dir)
        influx = None
        if settings.influx is not None:
            influx = InfluxdbStore(
                settings.influx.token,
                settings.influx.write_url,
                settings.influx.read_url,
                settings.influx.org,
            )

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
            selection_settings=settings.selection,
        )
        quiet_strategy_logs(runner.profile.strategy_module_name, settings.strategy_log_level)
        runner.run()
        if args.optimize:
            runner.post_analysis()
        if runner.failed:
            LOG.error(f"{len(runner.failed)} backtest(s) failed; their results were not saved")
            return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted")
        return 130
    except Exception:
        LOG.exception("Run failed")
        return 1
    finally:
        env.leave()
    return 0


def cmd_results(args) -> int:
    env = Env(sys_env.get_env_root_dir(), args.env)
    env.visit()
    rows = []
    for path in run_dir.iter_complete(Path(env.work_dir)):
        try:
            rows.append({**run_dir.read_json(path, run_dir.META), "result": run_dir.read_json(path, run_dir.SUMMARY)})
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
