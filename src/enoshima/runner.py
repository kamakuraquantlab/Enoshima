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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from logging import getLogger
from pathlib import Path

from qate.core.features import FEATURES
from qate.trading.param import ParamGrid
from qate.util.dt_range import DtRange
from qate_env.env import Env
from qate_env.run_log import RunLogger
from qate_env.trading_env import (
    PARAM_GRID_FILE,
    get_strategy_class,
    load_trading_env,
)

from .backtest import Backtest
from .data_source import DEFAULT_CHUNK_DAYS, DataSource, market_data_event_types
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
        influx=None,
    ):
        """`influx` is an `influx.InfluxdbStore`, or None to record no metric stream."""
        self.env = env
        self.dt_range = dt_range
        self.optimize = optimize
        self.chunk_days = chunk_days
        self.max_concurrency = max_concurrency
        self.force = force
        self.output_root = Path(env.work_dir)
        self.influx = influx
        self.batch_id = self._next_batch_id(self.output_root)

        self.definition = load_trading_env(env)
        FEATURES.load_from_dict(self.definition.features)
        self.config = self.definition.config
        self.profile = self.definition.profile

        self.run_logger = RunLogger(str(self.output_root))

    @staticmethod
    def _next_batch_id(output_root: Path) -> int:
        """A unix timestamp, advanced until it names a batch that does not exist.

        The id groups one sweep's runs together and names its `best_result` file,
        so two sweeps sharing an id would overwrite each other's best. A bare
        `int(time.time())` collides whenever two runs start inside the same
        second, which is ordinary when they are launched from a script.
        """
        best_root = output_root / "best_result"
        batch_id = int(time.time())
        while (best_root / f"{batch_id}.json").exists():
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
            self._run_batch(backtests)
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
        param_set_id = strategy.get_param_set_id()

        run_log = BacktestLog(
            self.env,
            params,
            self.dt_range,
            self.profile.strategy_name,
            batch_id=self.batch_id,
            config=self.config,
            profile=self.profile,
        )
        run_log.param_set_id = param_set_id

        if not self.force and self.run_logger.get(run_log):
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
        from .influx import InfluxReporter, bucket_name

        bucket = bucket_name(str(self.env.name), param_set_id)
        run_log.influx_bucket = bucket
        writer = self.influx.create_writer(bucket, param_set_id=param_set_id)
        return [InfluxReporter(writer, self.dt_range)]

    def _run_batch(self, backtests: list[Backtest]) -> None:
        """Run a batch concurrently, and do not let one failure hang the rest.

        Every backtest shares one buffer and the last to finish a chunk loads the
        next, so a thread that dies without draining its channel would leave the
        others waiting forever. Collecting each future's exception is what turns
        that deadlock into a reported failure.
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
                        continue
                    self.run_logger.save(backtest.run_log)
                    self._log_result(backtest.run_log)
            except KeyboardInterrupt:
                LOG.warning("Interrupted; stopping backtests")
                for backtest in backtests:
                    backtest.stop()
                raise

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
        """Rank every result for this strategy and range, and save the best.

        Ranked on `pnl5` — the middle slippage assumption, not the optimistic
        one. Picking a winner on the zero-slippage figure selects for strategies
        whose edge is thinnest.

        Ranked across every run log matching the strategy and the date range,
        *not* only the ones this batch ran. That distinction is what makes a
        resumed sweep give the right answer: the parameter sets skipped because
        they already had a result are still candidates for best. Ranking by batch
        id instead meant an interrupted sweep, resumed, chose its winner from only
        the combinations it happened to re-run.
        """
        if not self.optimize:
            return None

        run_log_dir = Path(self.run_logger.root_dir)
        if not run_log_dir.is_dir():
            LOG.warning(f"No run logs at {run_log_dir}")
            return None

        results = []
        for json_file in run_log_dir.glob("*.json"):
            try:
                data = json.loads(json_file.read_text())
            except (json.JSONDecodeError, OSError) as e:
                LOG.error(f"Could not read {json_file}: {e}")
                continue
            if not self._is_comparable(data):
                continue
            if "result" in data and "pnl5" in data["result"]:
                results.append((data["result"]["pnl5"], data))

        if not results:
            LOG.warning(
                f"No results with a pnl5 figure for {self.profile.strategy_name} "
                f"over {self.dt_range.start_str}..{self.dt_range.end_str}"
            )
            return None

        results.sort(key=lambda x: x[0], reverse=True)
        best_pnl, best = results[0]

        best_dir = self.output_root / "best_result"
        best_dir.mkdir(parents=True, exist_ok=True)
        best_path = best_dir / f"{self.batch_id}.json"
        best_path.write_text(json.dumps(best, indent=2))

        params = " ".join(f"{k}={v}" for k, v in best.get("params", {}).items())
        sharpe = best.get("result", {}).get("sharpe_ratio_annualized0")
        LOG.info(
            f"DONE {len(results)} combos - BEST pnl5={_fmt(best_pnl, '+.2f')} "
            f"trades={best.get('result', {}).get('pnl_cnt0')} "
            f"sharpe={_fmt(sharpe, '.3f')} "
            f"params=({params}) saved={best_path}"
        )
        return best
