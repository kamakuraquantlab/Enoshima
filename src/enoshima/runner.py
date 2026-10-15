"""Orchestration: which parameter sets to run, and replaying the data once for all of them.

A single backtest is `backtest.Backtest`. This is what decides how many of them
there are and feeds them.

## Why the batching exists

An optimize sweep is a hundred backtests over the same days. The data is the
expensive part — reading and materialising order books, not running the strategy
— so it is read once and shared, and the parameter sets run concurrently against
it. `MAX_CONCURRENCY` sets how many share one pass; a sweep larger than that is
split into batches, each of which replays the range again.

So the cost of a sweep is `ceil(combos / MAX_CONCURRENCY)` passes over the data,
not `combos` passes.

## Which parameters a single run uses

`params.json`, the same file a live run reads. That is what makes "backtest exactly
what production is running" one command rather than a comparison of two files.

It was `param_grid.json`'s default values, which meant an environment had two
sources of truth for its parameters and nothing kept them in step -- in practice
the backtest envs left `params.json` empty and the live envs left the grid alone.
A grid is a search space, an input to `--optimize`, not a description of the
environment.

## Warmup

Every run is fed `WARMUP_HOURS` of data from before the requested range so
indicators and charts have history before anything trades. Those events are
replayed but not reported — see `result.in_range`. Without it the
first hours of every backtest measure a strategy whose indicators are still
converging.
"""

import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from logging import getLogger
from pathlib import Path

from qate.core.features import FEATURES
from qate.trading.param import ParamGrid
from qate.util.dt_range import DtRange
from qate_env.env import Env
from qate_env.trading_env import (
    PARAM_GRID_FILE,
    get_strategy_class,
    load_trading_env,
)

from . import param_id, run_dir, selection
from .backtest import Backtest
from .data_source import DEFAULT_CHUNK_DAYS, DataSource, market_data_event_types
from .influx import InfluxdbStore, InfluxReporter, bucket_name
from .result import ResultReporter
from .run_log import BacktestLog

LOG = getLogger(__name__)

WARMUP_HOURS = 4
MAX_CONCURRENCY = 16

MODE_SINGLE = None
MODE_GRID = "grid"
MODE_COMBO = "combo"

COMBO_FILE = "params_combo.json"


def _fmt(value: float | None, spec: str) -> str:
    """Format a result figure, or "n/a" when the run produced none.

    A run with no closed position has no PnL and no Sharpe, and that is a real
    outcome rather than a zero: printing 0.00 would make a strategy that never
    traded look like one that broke even.
    """
    return "n/a" if value is None else format(value, spec)


class Runner:
    def __init__(
        self,
        env: Env,
        dt_range: DtRange,
        optimize: str | None = MODE_SINGLE,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        max_concurrency: int = MAX_CONCURRENCY,
        force: bool = False,
        influx: InfluxdbStore | None = None,
        temporary: bool = False,
        selection_settings: selection.SelectionSettings | None = None,
    ):
        """`influx` is an `influx.InfluxdbStore`, or None to record no metric stream.

        `temporary` gives every run of this batch a bucket of its own, named with the
        batch's timestamp; see `param_id`. It always runs: a fresh
        bucket is the point, so an existing result is no reason to skip.
        """
        self.env = env
        self.dt_range = dt_range
        self.optimize = optimize
        self.chunk_days = chunk_days
        self.max_concurrency = max_concurrency
        self.force = force
        self.output_root = Path(env.work_dir)
        self.influx = influx
        self.temporary = temporary
        self.selection_settings = selection_settings or selection.SelectionSettings()
        self.batch_id = self._next_batch_id(self.output_root)
        # The parameter sets whose backtest raised. A run with any is a failed
        # run, whatever else it computed: `enoshima run` exits non-zero.
        self.failed: list[dict] = []

        self.definition = load_trading_env(env)
        FEATURES.load_from_dict(self.definition.features)
        self.config = self.definition.config
        self.profile = self.definition.profile

    @staticmethod
    def _next_batch_id(output_root: Path) -> int:
        """A unix timestamp, advanced until it names a batch that does not exist.

        The id groups one sweep's runs together and names its `best_result`
        folder, so two sweeps sharing an id would overwrite each other's choice. A bare
        `int(time.time())` collides whenever two runs start inside the same
        second, which is ordinary when they are launched from a script.
        """
        best_root = output_root / "best_result"
        batch_id = int(time.time())
        while (best_root / str(batch_id)).exists():
            batch_id += 1
        return batch_id

    def param_sets(self) -> list[dict]:
        if self.optimize == MODE_GRID:
            return self._param_grid().get_all()
        if self.optimize == MODE_COMBO:
            combo_file = Path(self.env.work_dir) / COMBO_FILE
            if not combo_file.is_file():
                raise RuntimeError(f"--optimize combo needs {combo_file}")
            return json.loads(combo_file.read_text())
        return [self._single_params()]

    def _param_grid(self) -> ParamGrid:
        if self.definition.param_grid is None:
            raise RuntimeError(f"--optimize needs {PARAM_GRID_FILE} in {self.env.work_dir}")
        return ParamGrid.from_object(self.definition.param_grid)

    def _single_params(self) -> dict:
        if self.definition.params:
            return self.definition.params
        param_grid = self._param_grid()
        return param_grid.get_default()

    def run(self) -> None:
        combos = self.param_sets()
        LOG.info(
            f"START env={self.env.name} strategy={self.profile.strategy_name} "
            f"range={self.dt_range.start_str}..{self.dt_range.end_str} "
            f"mode={self.optimize or 'single'} combos={len(combos)}"
        )

        markets = [(m, market_data_event_types(t)) for m, t in self.config.get_markets()]
        data_source = DataSource(
            DtRange(self.dt_range.start_dt - timedelta(hours=WARMUP_HOURS), self.dt_range.end_dt),
            self.chunk_days,
        )
        for market, event_types in markets:
            data_source.add_source(market, event_types)

        self._warn_about_gaps(data_source, markets)

        n_batches = (len(combos) + self.max_concurrency - 1) // self.max_concurrency
        for i in range(0, len(combos), self.max_concurrency):
            batch_num = i // self.max_concurrency + 1
            data_source.reset()

            backtests = []
            for params in combos[i : i + self.max_concurrency]:
                backtest = self._create_backtest(data_source, params)
                if backtest:
                    backtests.append(backtest)

            if not backtests:
                LOG.info(f"Batch {batch_num}/{n_batches}: nothing to do, every result exists")
                continue

            LOG.info(f"Batch {batch_num}/{n_batches}: running {len(backtests)} backtest(s)")
            self._run_batch(backtests, data_source)
            LOG.info(f"Batch {batch_num}/{n_batches}: done")

    def _warn_about_gaps(self, data_source: DataSource, markets) -> None:
        missing = data_source.check_coverage(markets)
        if not missing:
            return
        LOG.warning(
            f"{len(missing)} market-day(s) of the requested range are not on disk. "
            f"A missing day replays as empty, so the result covers less than it appears to. "
            f"Download them with komachi, or narrow the range."
        )
        for item in missing[:20]:
            LOG.warning(f"  missing: {item}")
        if len(missing) > 20:
            LOG.warning(f"  ... and {len(missing) - 20} more")

    def _create_backtest(self, data_source: DataSource, params: dict) -> Backtest | None:
        StrategyClass = get_strategy_class(self.profile.strategy_module_name, self.profile.variant)
        strategy = StrategyClass(self.config, params)
        param_set_id = param_id.param_set_id(
            str(self.env.name),
            params,
            self.definition.param_grid,
            tmp_ts=self.batch_id if self.temporary else None,
        )

        run_log = BacktestLog(
            self.env,
            params,
            self.dt_range,
            self.profile.strategy_name,
            is_temporary_run=self.temporary,
            batch_id=self.batch_id,
            config=self.config,
            profile=self.profile,
        )
        run_log.param_set_id = param_set_id

        if not self.force and not self.temporary and run_dir.is_complete(run_dir.run_dir(self.output_root, run_log.get_id())):
            LOG.info(f"Skipping {param_set_id}: a result for this range already exists (--force to redo)")
            return None

        reporters = self._recording_reporters(run_log, param_set_id)
        result = ResultReporter(self.dt_range)
        return Backtest(
            strategy,
            self.config.get_exchanges(),
            data_source.create_channel(),
            result,
            run_log,
            reporters,
        )

    def _recording_reporters(self, run_log: BacktestLog, param_set_id: str) -> list:
        if self.influx is None:
            return []

        bucket = bucket_name(param_set_id)
        run_log.influx_bucket = bucket
        writer = self.influx.create_writer(bucket, param_set_id=param_set_id)
        return [InfluxReporter(writer, self.dt_range)]

    def _run_batch(self, backtests: list[Backtest], data_source: DataSource) -> None:
        """Run a batch concurrently, and do not let one failure hang the rest.

        Every backtest waits for the others at the end of each chunk. One that
        fails releases its place (`Channel.close`), so the rest finish without
        it; its exception is collected here and the run is marked failed.
        Interrupted, the data source is aborted first: a backtest waiting at
        the barrier is not reading its queue, so stopping its trader alone
        would leave it -- and the process -- waiting.
        """
        with ThreadPoolExecutor(max_workers=len(backtests), thread_name_prefix="backtest-") as executor:
            futures = {executor.submit(b.run): b for b in backtests}
            try:
                for future in as_completed(futures):
                    backtest = futures[future]
                    try:
                        future.result()
                    except Exception:
                        LOG.exception(f"Backtest failed: params={backtest.run_log.params}")
                        self.failed.append(backtest.run_log.params)
                        continue
                    self._save(backtest)
                    self._log_result(backtest.run_log)
            except KeyboardInterrupt:
                LOG.warning("Interrupted; stopping backtests")
                data_source.abort()
                for backtest in backtests:
                    backtest.stop()
                raise

    def _save(self, backtest: Backtest) -> None:
        log = backtest.run_log
        path = run_dir.write(self.output_root, log.get_id(), log.to_dict(), log.result, backtest.result.trades)
        LOG.info(f"Saved {path}")

    def _log_result(self, run_log: BacktestLog) -> None:
        result = run_log.result or {}
        params = " ".join(f"{k}={v}" for k, v in (run_log.params or {}).items())
        pnl = result.get("pnl5")
        sharpe = result.get("sharpe_ratio_annualized0")
        LOG.info(
            f"RESULT params=({params}) "
            f"pnl5={_fmt(pnl, '+.2f')} "
            f"trades={result.get('pnl_cnt0')} "
            f"sharpe={_fmt(sharpe, '.3f')}"
        )

    # ----------------------------------------------------------- post-analysis

    def _is_comparable(self, run_log_data: dict) -> bool:
        """Whether a recorded run answers the same question as this one.

        Same strategy and variant, same range. Two runs that differ in either are
        not candidates for the same "best parameter set", because the number they
        report is not measuring the same thing.
        """
        return (
            run_log_data.get("strategy") == self.profile.strategy_name
            and run_log_data.get("start_str") == self.dt_range.start_str
            and run_log_data.get("end_str") == self.dt_range.end_str
        )

    def post_analysis(self) -> dict | None:
        """Choose a parameter set from every result for this strategy and range, and save it.

        See `selection`: sets that lose money at the pessimistic slippage (or
        break the env's other filters) rank last, a set's score is pnl5
        averaged with its grid neighbours, and -- when the run logs carry
        months -- the choice is walk-forward tested, and the set recommended is
        the one chosen on the most recent months.

        Ranked across every run log matching the strategy and the date range,
        *not* only the ones this batch ran. That distinction is what makes a
        resumed sweep give the right answer: the parameter sets skipped because
        they already had a result are still candidates. Ranking by batch id
        instead meant an interrupted sweep, resumed, chose its winner from only
        the combinations it happened to re-run.
        """
        if not self.optimize:
            return None

        runs = []
        for path in run_dir.iter_complete(self.output_root):
            try:
                meta = run_dir.read_json(path, run_dir.META)
                summary = run_dir.read_json(path, run_dir.SUMMARY)
                months = run_dir.read_json(path, run_dir.grouping_file("month"))
            except (json.JSONDecodeError, OSError) as e:
                LOG.error(f"Could not read {path}: {e}")
                continue
            if self._is_comparable(meta) and "pnl5" in summary:
                runs.append({**meta, "run_id": path.name, "result": {**summary, "periods": months}})

        if not runs:
            LOG.warning(
                f"No results with a pnl5 figure for {self.profile.strategy_name} "
                f"over {self.dt_range.start_str}..{self.dt_range.end_str}"
            )
            return None

        grid = self.definition.param_grid if self.optimize == MODE_GRID else None
        choice = selection.select(runs, self.dt_range.start_ts, self.dt_range.end_ts, grid, self.selection_settings)
        self._log_choice(choice, len(runs))

        top = choice["ranking"][0]["params"]
        best = next(r for r in runs if r["params"] == top)
        best = {**best, "result": {k: v for k, v in best["result"].items() if k != "periods"}, "selection": choice}

        # The choice and why, and -- only when one passed the filters -- the
        # chosen run's meta.json, under its run id.
        best_dir = self.output_root / "best_result" / str(self.batch_id)
        best_dir.mkdir(parents=True, exist_ok=True)
        (best_dir / "selection.json").write_text(json.dumps(choice, indent=2))
        if choice["recommended"] is not None:
            shutil.copyfile(
                run_dir.run_dir(self.output_root, best["run_id"]) / run_dir.META, best_dir / f"{best['run_id']}.json"
            )
        LOG.info(f"Saved {best_dir}")
        return best

    def _log_choice(self, choice: dict, n_runs: int) -> None:
        def show(params):
            return " ".join(f"{k}={v}" for k, v in (params or {}).items()) or "none"

        if choice["method"] == "walk-forward":
            wf = choice["walk_forward"]
            for fold in wf["folds"]:
                LOG.info(
                    f"FOLD train {fold['train'][0]}..{fold['train'][1]} test {fold['test'][0]}..{fold['test'][1]}: "
                    f"pnl5={_fmt(fold['pnl5'], '+.2f')} params=({show(fold['params'])})"
                )
            oos = wf["out_of_sample"]
            LOG.info(
                f"WALK-FORWARD {oos['folds']} folds, out of sample: pnl0={_fmt(oos['pnl0'], '+.2f')} "
                f"pnl5={_fmt(oos['pnl5'], '+.2f')} pnl10={_fmt(oos['pnl10'], '+.2f')} "
                f"profitable folds {oos['profitable_folds']}/{oos['folds']}"
            )
        else:
            LOG.info(f"Ranked on the whole range: {choice['reason']}")

        top = choice["ranking"][0]
        if choice["recommended"] is None:
            LOG.warning(
                f"No parameter set passes the filters ({choice['settings']}); nothing recommended. "
                f"Highest score: params=({show(top['params'])}) pnl5={_fmt(top['pnl5'], '+.2f')}"
            )
            return
        LOG.info(
            f"DONE {n_runs} combos - RECOMMENDED score={_fmt(top['score'], '+.2f')} "
            f"pnl5={_fmt(top['pnl5'], '+.2f')} pnl10={_fmt(top['pnl10'], '+.2f')} "
            f"trades={top['trades']} max_dd5={_fmt(top['dd5'], '.2f')} params=({show(top['params'])})"
        )
