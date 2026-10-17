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

`strategy_param_grid.json` holds both the defaults for one run and the values an
optimize sweep expands. It also names the one strategy module and variant being
measured.

## Warmup

Every run is fed `WARMUP_HOURS` of data from before the requested range so
indicators and charts have history before anything trades. Those events are
replayed but not reported — see `result.in_range`. Without it the
first hours of every backtest measure a strategy whose indicators are still
converging.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from logging import getLogger
from pathlib import Path

from qate.trading.param import ParamGrid
from qate.util.dt_range import DtRange
from qate_env.env import Env

from . import best, param_id, run_dir, selection
from .backtest import Backtest
from .data_source import DEFAULT_CHUNK_DAYS, DataSource, market_data_event_types
from .influx import InfluxdbStore, InfluxdbWriter, InfluxReporter, bucket_name
from .result import ResultReporter
from .run_log import BacktestLog
from .strategy_env import get_strategy_class, load_strategy_env

LOG = getLogger(__name__)

_last_batch_id = 0  # see `Runner._next_batch_id`

WARMUP_HOURS = 4
MAX_CONCURRENCY = 16

MODE_SINGLE = None
MODE_GRID = "grid"


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
        selection_settings: selection.SelectionSettings | None = None,
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
        self.selection_settings = selection_settings or selection.SelectionSettings()
        self.batch_id = self._next_batch_id(self.output_root)
        # The parameter sets whose backtest raised. A run with any is a failed
        # run, whatever else it computed: `enoshima run` exits non-zero.
        self.failed: list[dict] = []

        strategy_env = load_strategy_env(env)
        self.definition = strategy_env.definition
        self.config = strategy_env.config

    @staticmethod
    def _next_batch_id(output_root: Path) -> int:
        """A unix timestamp, advanced until it names a batch that does not exist.

        The id groups one sweep's runs together, names its `best_result` folder and
        ends each of its InfluxDB buckets, so two sweeps sharing an id would overwrite
        each other's choice and write into each other's buckets. A bare
        `int(time.time())` collides whenever two runs start inside the same
        second, which is ordinary when they are launched from a script.

        Only a sweep writes a `best_result` folder, so a single run leaves nothing
        on disk to advance past; the last id this process issued covers the
        common case of two runs started one after the other.
        """
        global _last_batch_id
        best_root = output_root / "best_result"
        batch_id = max(int(time.time()), _last_batch_id + 1)
        while (best_root / str(batch_id)).exists():
            batch_id += 1
        _last_batch_id = batch_id
        return batch_id

    def param_sets(self) -> list[dict]:
        if self.optimize == MODE_GRID:
            return self._param_grid().get_all()
        return [self._single_params()]

    def _param_grid(self) -> ParamGrid:
        return ParamGrid.from_object(self.definition.params)

    def _single_params(self) -> dict:
        return self._param_grid().get_default()

    def run(self) -> None:
        combos = self.param_sets()
        LOG.info(
            f"START env={self.env.name} strategy={self.definition.strategy_name} "
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
        StrategyClass = get_strategy_class(self.definition.module_name, self.definition.variant_name)
        strategy = StrategyClass(self.config, params)
        param_set_id = param_id.param_set_id(str(self.env.name), params, self.definition.params)

        run_log = BacktestLog(
            self.env,
            params,
            self.dt_range,
            self.definition.strategy_name,
            batch_id=self.batch_id,
            config=self.config,
            profile={"module_name": self.definition.module_name, "variant_name": self.definition.variant_name},
        )
        run_log.param_set_id = param_set_id

        if not self.force and run_dir.is_complete(run_dir.run_dir(self.output_root, run_log.get_id())):
            LOG.info(f"Skipping {param_set_id}: a result for this range already exists (--force to redo)")
            return None

        writer, reporters = self._recording(run_log, param_set_id)
        result = ResultReporter(self.dt_range, writer)
        return Backtest(
            strategy,
            self.config.get_exchanges(),
            data_source.create_channel(),
            result,
            run_log,
            reporters,
        )

    def _recording(self, run_log: BacktestLog, param_set_id: str) -> tuple[InfluxdbWriter | None, list[InfluxReporter]]:
        if self.influx is None:
            return None, []

        bucket = bucket_name(param_set_id, self.batch_id)
        run_log.influx_bucket = bucket
        writer = self.influx.create_writer(bucket, param_set_id=param_set_id)
        return writer, [InfluxReporter(writer, self.dt_range)]

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

    def post_analysis(self) -> list[dict] | None:
        """Recommend for every range and group in this environment; see `best`.

        Every run folder is a candidate, *not* only the ones this batch ran.
        That is what makes a resumed sweep give the right answer: the parameter
        sets skipped because they already had a result are still candidates.
        Ranking by batch id instead meant an interrupted sweep, resumed, chose
        its winner from only the combinations it happened to re-run.
        """
        if not self.optimize:
            return None
        groups = best.recommend(self.output_root, self.batch_id, self.selection_settings)
        if not groups:
            LOG.warning(f"No results with a pnl5 figure in {self.output_root}")
            return None
        return groups
