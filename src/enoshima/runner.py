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

## Warmup

Every run is fed `WARMUP_HOURS` of data from before the requested range so
indicators and charts have history before anything trades. Those events are
replayed but not reported — see `ResultCollector._should_emit`. Without it the
first hours of every backtest measure a strategy whose indicators are still
converging.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from logging import getLogger
from pathlib import Path

from qate.boot.config import BootConfig, TradingProfile
from qate.boot.run_log import RunLogger
from qate.boot.strategy_loader import get_strategy_class
from qate.core.features import FEATURES
from qate.env.env import Env
from qate.trading.param import ParamGrid
from qate.util.dt_range import DtRange

from .backtest import Backtest
from .bronze import BronzeStore
from .data_source import DEFAULT_CHUNK_DAYS, DataSource, market_data_event_types
from .result import ResultCollector
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
        data_root: str | None = None,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        max_concurrency: int = MAX_CONCURRENCY,
        force: bool = False,
    ):
        self.env = env
        self.dt_range = dt_range
        self.optimize = optimize
        self.chunk_days = chunk_days
        self.max_concurrency = max_concurrency
        self.force = force
        self.batch_id = self._next_batch_id(env)

        FEATURES.load_from_dict(self.env.get("features"))
        self.config: BootConfig = self.env.get("config")
        self.profile: TradingProfile = self.env.get("trading")
        if self.config is None or self.profile is None:
            raise RuntimeError(
                f"Environment {self.env.name} has no config.json or trading.json. "
                f"See the example environment in the Enoshima repository."
            )

        self.store = BronzeStore(data_root)
        self.run_logger = RunLogger(str(self.env.work_dir))

    @staticmethod
    def _next_batch_id(env: Env) -> int:
        """A unix timestamp, advanced until it names a batch that does not exist.

        The id groups one sweep's runs together and `post_analysis` ranks within
        it, so two sweeps sharing an id would have their results merged and a
        "best parameter set" chosen across both. A bare `int(time.time())`
        collides whenever two runs start inside the same second, which is
        ordinary when they are launched from a script.
        """
        result_root = Path(env.work_dir) / "result"
        batch_id = int(time.time())
        while (result_root / str(batch_id)).exists():
            batch_id += 1
        return batch_id

    # ---------------------------------------------------------------- planning

    def param_sets(self) -> list[dict]:
        param_grid = ParamGrid.from_object(self.env.get("param_grid"))
        if self.optimize == MODE_GRID:
            return param_grid.get_all()
        if self.optimize == MODE_COMBO:
            combo_file = Path(self.env.work_dir) / COMBO_FILE
            if not combo_file.is_file():
                raise RuntimeError(f"--optimize combo needs {combo_file}")
            return json.loads(combo_file.read_text())
        return [param_grid.get_default()]

    def result_dir(self, param_set_id: str) -> Path:
        return Path(self.env.work_dir) / "result" / str(self.batch_id) / param_set_id

    # ----------------------------------------------------------------- running

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
            self.store,
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
            str(self.result_dir(param_set_id)),
            params,
            self.dt_range,
            self.profile.strategy_name,
            batch_id=self.batch_id,
        )
        run_log.param_set_id = param_set_id

        if not self.force and self.run_logger.get(run_log):
            LOG.info(f"Skipping {param_set_id}: a result for this range already exists (--force to redo)")
            return None

        result = ResultCollector(
            self.result_dir(param_set_id),
            self.dt_range,
            self.profile.strategy_name,
            param_set_id,
        )
        return Backtest(
            strategy,
            self.config.get_exchanges(),
            data_source.create_channel(),
            result,
            run_log,
        )

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
                    backtest.trader.stop()
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

        best_dir = Path(self.env.work_dir) / "best_result"
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
