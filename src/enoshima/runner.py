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

## Weekly runs

With `weekly`, the range is widened to the ISO weeks covering it, clipped to
the complete days on disk, and each week is replayed on its own -- one pass
over the week, its preload and its overlap for every parameter set that needs
it, in batches as above. A week already written is skipped unless the data now
reaches further into it than when it ran; the weeks between those on disk and
those asked for are filled in, so each parameter set's weeks stay contiguous.
After each week the run's `BacktestRun` row is rebuilt from all of its weeks
(`weekly.rebuild`). See `weekly`.

Any other run is temporary: a quick look at some days, with its own summary,
and not a candidate for `enoshima select`.

## Where a run writes

Into the data lake, `lake.Store`: each finished backtest's part -- the week,
or the whole range of a temporary run -- with its trades and its records,
then the run's row. Nothing goes to the environment directory but `env.log`.

## Choosing is not part of a run

A run writes results and its summaries, and nothing else. Recommending a
parameter set -- `enoshima select` -- and testing the rule that does it --
`enoshima walk-forward` -- are asked for, when there are results to choose from.
"""

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from logging import getLogger
from pathlib import Path

from qate.exchange import FeeSchedule
from qate.trading.param import ParamGrid
from qate.util.dt_range import DtRange
from qate_env.env import Env

from . import fees, lake, param_id
from . import weekly as weekly_mod
from .backtest import Backtest
from .data_source import DEFAULT_CHUNK_DAYS, DataSource, Stream, market_data_event_types
from .records import RecordReporter
from .result import ResultReporter
from .run_log import BacktestLog
from .strategy_env import get_strategy_class, load_strategy_env, strategy_commit, strategy_rev
from .weekly import Week, WeeklySettings

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
        weekly: WeeklySettings | None = None,
        fee_rates: dict[str, FeeSchedule] | None = None,
        lake_root: Path | None = None,
    ):
        """`fee_rates` is `enoshima.json`'s, by market id. Every market the strategy
        trades must have one, or this raises before anything is replayed; see `fees`.

        `weekly`, given, makes this a strict weekly run: `dt_range` is widened
        to the weeks that cover it, and `weeks` lists them.

        `lake_root` is where results are written, the data lake by default. It
        is not the command's to choose; a tool comparing two runs of one env
        gives each its own, so that neither writes over the env's results.
        """
        self.env = env
        self.weekly = weekly
        self.weeks: list[Week] = weekly_mod.covering(dt_range) if weekly else []
        if weekly:
            dt_range = DtRange(self.weeks[0].start, self.weeks[-1].dt_range.end_dt)
        self.dt_range = dt_range
        self.optimize = optimize
        self.chunk_days = chunk_days
        self.max_concurrency = max_concurrency
        self.force = force
        self.store = lake.Store(lake_root or lake.lake_root(), str(env.name))
        self.batch_id = self._next_batch_id(self.store.analysis_dir)
        # The parameter sets whose backtest raised. A run with any is a failed
        # run, whatever else it computed: `enoshima run` exits non-zero.
        self.failed: list[dict] = []

        strategy_env = load_strategy_env(env)
        self.definition = strategy_env.definition
        self.config = strategy_env.config
        # Part of every run's id: a change to the variant's source is a new folder.
        self.strategy_rev = strategy_rev(self.definition.module_name, self.definition.variant_name)
        # Provenance only: which commit that source was run from.
        self.strategy_commit = strategy_commit(self.definition.module_name)
        self.fee_rates = fees.check(fee_rates or {}, self.config)
        fees.register(self.fee_rates)

    @staticmethod
    def _next_batch_id(analysis_dir: Path) -> int:
        """A unix timestamp, advanced until it names a batch that does not exist.

        The id groups one sweep's runs together and names its `best_result`
        folder, so two sweeps sharing an id would overwrite each other's
        choice. A bare
        `int(time.time())` collides whenever two runs start inside the same
        second, which is ordinary when they are launched from a script.

        Only `select` writes a `best_result` folder, so a run leaves nothing
        on disk to advance past; the last id this process issued covers the
        common case of two runs started one after the other.
        """
        global _last_batch_id
        best_root = Path(analysis_dir) / "best_result"
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
        mode = self.optimize or "single"
        if self.weekly:
            mode += (f" weekly({len(self.weeks)} weeks, preload {self.weekly.preload_days}d,"
                     f" overlap {self.weekly.overlap_days}d)")
        LOG.info(
            f"START env={self.env.name} strategy={self.definition.strategy_name} "
            f"range={self.dt_range.start_str}..{self.dt_range.end_str} "
            f"mode={mode} combos={len(combos)}"
        )
        if not self.weekly:
            self._run_range(combos, self.dt_range, timedelta(hours=WARMUP_HOURS))
            return
        available = self._available_days()
        if available is None:
            LOG.warning("No day is complete on disk for every stream the strategy reads; nothing to run")
            return
        LOG.info(f"Complete days on disk: {available[0]}..{available[1]}")
        if available[1] - self.weekly.overlap < available[0]:
            LOG.warning(f"No day on disk has its {self.weekly.overlap_days}-day overlap after it; nothing to run")
            return
        todo = self._weeks_to_run(combos, available)
        for week in sorted(todo, key=lambda w: w.monday):
            dt_range, params = todo[week]
            part = "" if dt_range.start_dt == week.start and dt_range.end_dt == week.dt_range.end_dt else \
                f" (partial: {dt_range.start_str}..{dt_range.end_str})"
            LOG.info(f"Week {week.key}{part}: {len(params)} parameter set(s)")
            self._run_range(params, dt_range, self.weekly.preload, week, self.weekly.overlap)
        self._rebuild_all(combos)

    def _available_days(self) -> tuple[dt.date, dt.date] | None:
        """The first and last day complete on disk for every stream the strategy reads."""
        common: set[str] | None = None
        for market, event_types in self.config.get_markets():
            for event_type in market_data_event_types(event_types):
                days = set(Stream(market, event_type).available_dates())
                common = days if common is None else common & days
        if not common:
            return None
        return dt.date.fromisoformat(min(common)), dt.date.fromisoformat(max(common))

    def _weeks_to_run(
        self, combos: list[dict], available: tuple[dt.date, dt.date]
    ) -> dict[Week, tuple[DtRange, list[dict]]]:
        """Each week, the part of it with data, and the parameter sets that need it.

        The part with data ends `overlap_days` before the last complete day:
        a trade opened later could not be seen closed. A set needs a week
        asked for or between its weeks on disk, that it has no result for, or
        whose result the data now reaches past -- a week that was partial.
        `--force` reruns the weeks asked for.
        """
        todo: dict[Week, tuple[DtRange, list[dict]]] = {}
        grown = set()
        first, last = available
        last -= self.weekly.overlap
        for params in combos:
            run_id = self._run_log(params, self.weeks[0]).get_id()
            done = weekly_mod.on_disk(self.store, run_id)
            for week in weekly_mod.span(self.weeks, done):
                dt_range = week.clip(first, last)
                if dt_range is None:
                    continue
                if week in done and not (self.force and week in self.weeks):
                    start, end = weekly_mod.recorded_range(self.store, run_id, week)
                    if dt_range.start_str >= start and dt_range.end_str <= end:
                        continue
                    grown.add(week.key)
                todo.setdefault(week, (dt_range, []))[1].append(params)
        filled = sorted({w.key for w in todo} - {w.key for w in self.weeks})
        if filled:
            LOG.info(f"Filling {len(filled)} week(s) between those on disk and those asked for: {', '.join(filled)}")
        if grown:
            LOG.info(f"Running again {len(grown)} week(s) the data now reaches further into: {', '.join(sorted(grown))}")
        return todo

    def _run_log(self, params: dict, week: Week | None = None, dt_range: DtRange | None = None) -> BacktestLog:
        return BacktestLog(
            self.env,
            params,
            dt_range or week.dt_range,
            self.definition.strategy_name,
            batch_id=self.batch_id,
            config=self.config,
            profile={"module_name": self.definition.module_name, "variant_name": self.definition.variant_name},
            weekly=self.weekly if week else None,
            week_key=week.key if week else None,
            strategy_rev=self.strategy_rev,
            strategy_commit=self.strategy_commit,
            fee_rates=self.fee_rates,
        )

    def _run_range(
        self,
        combos: list[dict],
        dt_range: DtRange,
        warmup: timedelta,
        week: Week | None = None,
        overlap: timedelta = timedelta(),
    ) -> None:
        """Every parameter set over one reported range, fed `warmup` before it and `overlap` after."""
        markets = [(m, market_data_event_types(t)) for m, t in self.config.get_markets()]
        data_source = DataSource(
            DtRange(dt_range.start_dt - warmup, dt_range.end_dt + overlap),
            self.chunk_days,
        )
        for market, event_types in markets:
            data_source.add_source(market, event_types)

        self._warn_about_gaps(data_source, markets)

        n_batches = (len(combos) + self.max_concurrency - 1) // self.max_concurrency
        try:
            for i in range(0, len(combos), self.max_concurrency):
                batch_num = i // self.max_concurrency + 1
                data_source.reset()

                backtests = []
                for params in combos[i : i + self.max_concurrency]:
                    backtest = self._create_backtest(data_source, params, dt_range, week)
                    if backtest:
                        backtests.append(backtest)

                if not backtests:
                    LOG.info(f"Batch {batch_num}/{n_batches}: nothing to do, every result exists")
                    continue

                LOG.info(f"Batch {batch_num}/{n_batches}: running {len(backtests)} backtest(s)")
                self._run_batch(backtests, data_source)
                LOG.info(f"Batch {batch_num}/{n_batches}: done")
        finally:
            # The chunk-loader thread outlives a batch whose backtests all
            # stopped early -- a week's do, once its positions are closed: it
            # waits for a request that is not coming, holding the chunk it
            # loaded ahead and the one before. One left per week held about
            # 4 GB a week of a BTC sweep until the kernel killed it.
            data_source.reset()

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

    def _create_backtest(
        self, data_source: DataSource, params: dict, dt_range: DtRange, week: Week | None = None
    ) -> Backtest | None:
        StrategyClass = get_strategy_class(self.definition.module_name, self.definition.variant_name)
        strategy = StrategyClass(self.config, params)
        param_set_id = param_id.param_set_id(str(self.env.name), params, self.definition.params)

        run_log = self._run_log(params, week, dt_range)
        run_log.param_set_id = param_set_id

        # A weekly run's weeks were chosen by `_weeks_to_run`, which knows a partial week from a finished one.
        if not week and not self.force and self.store.is_complete(run_log.get_id()):
            LOG.info(f"Skipping {param_set_id}: a result for this range already exists (--force to redo)")
            return None

        records = RecordReporter(dt_range, by_open=week is not None)
        result = ResultReporter(dt_range, records, strict=week is not None)
        backtest = Backtest(
            strategy,
            self.config.get_exchanges(),
            data_source.create_channel(),
            result,
            run_log,
            [records],
        )
        backtest.records = records
        return backtest

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
        path = self.store.write_part(log.get_id(), log.week_key or lake.WHOLE, log.to_dict(), log.result,
                                     backtest.result.trades, backtest.records.tables())
        if log.week_key is None:
            self.store.write_run(log.get_id(), log.to_dict(), log.result, backtest.result.trades)
        else:
            # Every week, not once at the end: a run stopped part-way leaves a row that matches its weeks.
            weekly_mod.rebuild(self.store, log.get_id(), log.weekly_meta())
        LOG.info(f"Saved {path}")

    def _log_result(self, run_log: BacktestLog, result: dict | None = None, label: str | None = None) -> None:
        result = (run_log.result if result is None else result) or {}
        params = " ".join(f"{k}={v}" for k, v in (run_log.params or {}).items())
        pnl = result.get("pnl5")
        sharpe = result.get("sharpe_ratio_annualized0")
        label = label or run_log.week_key
        LOG.info(
            f"RESULT {label + ' ' if label else ''}params=({params}) "
            f"pnl5={_fmt(pnl, '+.2f')} "
            f"trades={result.get('pnl_cnt0')} "
            f"sharpe={_fmt(sharpe, '.3f')}"
        )

    def _rebuild_all(self, combos: list[dict]) -> None:
        """Rebuild every parameter set's root, ran or skipped, and log its figures over all its weeks."""
        for params in combos:
            run_log = self._run_log(params, self.weeks[0])
            run_log.param_set_id = param_id.param_set_id(str(self.env.name), params, self.definition.params)
            run_id = run_log.get_id()
            if not weekly_mod.rebuild(self.store, run_id, run_log.weekly_meta()):
                LOG.warning(f"{run_id}: its weeks have a gap, so it has no summary until a run fills it")
                continue
            run = self.store.read_run(run_id)
            meta = run["meta"]
            label = f"{meta['start_str'][:10]}..{meta['end_str'][:10]} ({meta['weeks']} weeks)"
            self._log_result(run_log, run["summary"], label)
