"""The `enoshima` command.

Run as: `enoshima <command>`, or from a checkout
`PYTHONPATH=src python -m enoshima.cli <command>`.

```
enoshima run     --env NAME --from 2026-06-01 --to 2026-06-10 [--weekly]
enoshima results --env NAME
enoshima migrate-revisions --env NAME [--apply]
enoshima merge   --env NAME --from RUN_ID --into RUN_ID [--unverified] [--apply]
enoshima import-run-log --env NAME [--apply]
enoshima web     [--setup]
```

`run` is the one that matters, and `run --weekly` is how it is mainly used: it
replays the ISO weeks (Monday to Sunday, JST) that cover the range, each on
its own, as far as there is data for them, and reuses every week already
computed, so a range extended later replays only its new weeks
(`enoshima.weekly`). A run without `--weekly` is a quick look at some days:
it has its summary, with its range and revision in its id.

A run writes results; it does not choose. Choosing a parameter set from them,
and testing how often to choose again, is research done on what the lake
holds, and is not Enoshima's: it runs backtests and shows their results.
`results` exists so that what a run produced can be found and read without
reading the source;
`web` serves the same results as a page (`enoshima.web`, the `web` extra).
`import-run-log` brings an env's run folders from before the lake into it
(`enoshima.legacy`).

Everything a run needs to know beyond the dates is somewhere already, so it is not
a flag here:

- **The data** is wherever komachi says it is: `ROOT_PATH` in komachi's settings
  file. `komachi local` lists it; to replay another tree, change it there.
- **The output** is the data lake, under the same root as the data: the
  `research/` layer's `Backtest*` datasets, partitioned by env and run id
  (`enoshima.lake`). The environment directory keeps its definition and `env.log`.
- **The strategy and variant** are `strategy_param_grid.json`'s. The strategy module is
  imported by name, so it must be installed or on `PYTHONPATH`.
- **How a run is costed and sized** is `enoshima.json`'s -- see
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
import signal
import sys
from logging import getLogger
from pathlib import Path

from qate.util.dt_range import DtRange
from qate_env import sys_env
from qate_env.env import Env
from qate_env.env_name import EnvName

from enoshima import legacy, revisions
from enoshima.lake import Store
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
        runner = Runner(
            env,
            dt_range,
            optimize=args.optimize,
            chunk_days=args.chunk_days or settings.chunk_days,
            max_concurrency=args.max_concurrency or settings.max_concurrency,
            force=args.force,
            weekly=settings.weekly if args.weekly else None,
            fee_rates=settings.fee_rates,
        )
        quiet_strategy_logs(runner.definition.module_name, settings.strategy_log_level)
        runner.run()
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
    store = Store.of(str(env.name))
    for run_id in store.run_ids():
        try:
            run = store.read_run(run_id)
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        rows.append({**run["meta"], "result": run["summary"]})
    if not rows:
        LOG.info(f"No runs recorded for {args.env}")
        return 1

    # Best first, on the middle slippage assumption.
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


def cmd_migrate_revisions(args) -> int:
    env = Env(sys_env.get_env_root_dir(), args.env)
    env.enter()
    try:
        revisions.migrate(Store.of(str(env.name)), apply=args.apply)
    except Exception:
        LOG.exception("migrate-revisions failed")
        return 1
    finally:
        env.leave()
    return 0


def cmd_merge(args) -> int:
    env = Env(sys_env.get_env_root_dir(), args.env)
    env.enter()
    try:
        revisions.merge(Store.of(str(env.name)), args.source, args.target, unverified=args.unverified, apply=args.apply)
    except Exception:
        LOG.exception("merge failed")
        return 1
    finally:
        env.leave()
    return 0


def cmd_import_run_log(args) -> int:
    env = Env(sys_env.get_env_root_dir(), args.env)
    env.enter()
    try:
        legacy.import_env(Path(env.work_dir), Store.of(str(env.name)), apply=args.apply)
    except Exception:
        LOG.exception("import-run-log failed")
        return 1
    finally:
        env.leave()
    return 0


def cmd_web(args) -> int:
    try:
        from enoshima import web
    except ImportError as e:
        sys.exit(f"enoshima web needs the web extra, pip install 'kamakuraquantlab-enoshima[web]': {e}")
    if args.setup:
        Env.setup_env(sys_env.get_env_root_dir(), web.ENV_NAME,
                      {"config": web.Config(), "grafana_dashboard": {"dashboards": []}})
        return 0
    env = Env(sys_env.get_env_root_dir(), web.ENV_NAME)
    env.enter()
    # A service manager stops it with SIGTERM; the finally releases the lock.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        web.serve(env)
    except KeyboardInterrupt:
        pass
    except Exception:
        LOG.exception("web failed")
        return 1
    finally:
        env.leave()
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
        choices=["grid"],
        default=None,
        help="run every combination in strategy_param_grid.json",
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
    run.add_argument(
        "--weekly",
        action="store_true",
        help="replay the whole ISO weeks (Mon-Sun JST) covering the range, one at a time, reusing weeks already run",
    )
    run.add_argument("--force", action="store_true", help="re-run what already has a result (with --weekly: the weeks asked for)")
    run.set_defaults(func=cmd_run)

    results = sub.add_parser("results", help="list recorded runs, best first")
    results.add_argument("--env", required=True, type=EnvName)
    results.add_argument("--limit", type=int, default=25)
    results.set_defaults(func=cmd_results)

    migrate = sub.add_parser(
        "migrate-revisions",
        help="give runs recorded at a commit the source revision of "
        "their variant at that commit, moving them to its id; a dry run without --apply",
    )
    migrate.add_argument("--env", required=True, type=EnvName)
    migrate.add_argument("--apply", action="store_true", help="rename and rewrite, rather than only report")
    migrate.set_defaults(func=cmd_migrate_revisions)

    merge = sub.add_parser(
        "merge",
        help="copy one weekly run's weeks into another of the same parameters "
        "and rebuild it; a dry run without --apply",
    )
    merge.add_argument("--env", required=True, type=EnvName)
    merge.add_argument("--from", dest="source", required=True, help="run id whose weeks are copied")
    merge.add_argument("--into", dest="target", required=True, help="run id they are copied into")
    merge.add_argument(
        "--unverified",
        action="store_true",
        help="merge although the two revisions cannot be shown to run the same source",
    )
    merge.add_argument("--apply", action="store_true", help="copy and rebuild, rather than only report")
    merge.set_defaults(func=cmd_merge)

    imp = sub.add_parser(
        "import-run-log",
        help="copy the env's run_log/ folders, from before results moved "
        "to the data lake, into it; a dry run without --apply",
    )
    imp.add_argument("--env", required=True, type=EnvName)
    imp.add_argument("--apply", action="store_true", help="write to the lake, rather than only report")
    imp.set_defaults(func=cmd_import_run_log)

    web = sub.add_parser("web", help="serve the recorded runs as a page, replaying nothing")
    web.add_argument("--setup", action="store_true", help="create the viewer's own env, ENOSHIMA_API")
    web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
