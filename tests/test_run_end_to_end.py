"""A real run, from parquet on disk to a run folder on disk.

Writes a synthetic bronze tree, copies the example environment into a temporary
env root, and drives `Runner` exactly as `enoshima run` does. If this passes, the
documented quick start works. InfluxDB is stood in for by a store that keeps its
points in memory: what is tested is what a run reports, not the client library.

The prices are a sine wave, so the fast and slow averages cross repeatedly and
the strategy actually opens and closes positions. A backtest that trades nothing
would satisfy a weaker test while telling us nothing, and getting this wrong is
easy: the first version of this file generated books that all fell before the
reported range began, so every one of them was warmup and nothing was recorded.
"""

import csv
import json
import math
import shutil
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hase.bronze import DataType
from hase.layout import data_path
from qate.core.model import ExchangeName, Market
from qate.core.symbol import Symbol
from qate.exchange import FeeSchedule
from qate.util.dt_range import DtRange
from qate_env.env import Env
from qate_env.env_name import EnvName

from enoshima import best, run_dir
from enoshima.ledger import GROUPINGS
from enoshima.runner import WARMUP_HOURS, Runner
from enoshima.selection import SelectionSettings
from enoshima.weekly import WeeklySettings

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent / "example"
ENV_NAME = EnvName("EXAMPLE_BACKTEST")
MARKET = Market(ExchangeName.COINCHECK, Symbol.BTC_SPOT)
# What the example env's enoshima.json states: Coincheck charges nothing.
FEES = {MARKET.id: FeeSchedule(maker=0.0, taker=0.0)}
DATE = "2026-01-02"
DEPTH = 3
SPREAD = 1000.0

# One book per second. The books must cover the warmup window *and* the reported
# range: the runner asks for WARMUP_HOURS before the range so indicators have
# history, and anything before the range start is replayed but never recorded.
RANGE_SECONDS = 3600
WARMUP_SECONDS = WARMUP_HOURS * 3600
SECONDS = WARMUP_SECONDS + RANGE_SECONDS + 1

# A 600-second cycle against a 120-second slow average gives a crossing every few
# minutes, so the reported hour contains several complete round trips. The
# amplitude is far wider than the spread, so a round trip is not swallowed by it.
CYCLE_SECONDS = 600
AMPLITUDE = 100_000.0
BASE_PRICE = 15_000_000.0


def price_path(n: int) -> float:
    return BASE_PRICE + AMPLITUDE * math.sin(2 * math.pi * n / CYCLE_SECONDS)


def write_books(root: Path, start_ts: float) -> None:
    columns = ["ts"]
    for side in ("bid", "ask"):
        for i in range(DEPTH):
            columns += [f"{side}{i}_price", f"{side}{i}_qty"]

    rows = []
    for n in range(SECONDS):
        mid = price_path(n)
        row = {"ts": start_ts + n}
        for i in range(DEPTH):
            row[f"bid{i}_price"] = mid - SPREAD / 2 - i * 100
            row[f"bid{i}_qty"] = 1.0
            row[f"ask{i}_price"] = mid + SPREAD / 2 + i * 100
            row[f"ask{i}_qty"] = 1.0
        rows.append(row)

    path = data_path(root, MARKET.id, DataType.ORDER_BOOK, DATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows, columns=columns), preserve_index=False), path)


def run_dirs(env) -> list[Path]:
    """Every finished run folder in the env."""
    return list(run_dir.iter_complete(Path(env.work_dir)))


def load(path: Path) -> dict:
    """A run folder as one dict: its meta.json, with summary.json as `result`."""
    return {**run_dir.read_json(path, run_dir.META), "result": run_dir.read_json(path, run_dir.SUMMARY)}


@pytest.fixture
def run_env(tmp_path, data_root, monkeypatch):
    """A temporary env root holding a copy of the example environment, and data.

    The books start `WARMUP_HOURS` before the reported range, because that is
    what the runner asks for: a range with no data before it would spend its
    first hours warming up on nothing.
    """
    env_root = tmp_path / "env"
    shutil.copytree(EXAMPLE_ROOT / "env", env_root)

    dt_range = DtRange.from_strings("20260102_0600", "20260102_0659")
    start_ts = dt_range.start_ts - WARMUP_SECONDS
    write_books(data_root, start_ts)

    env = Env(str(env_root), ENV_NAME)
    monkeypatch.chdir(tmp_path)
    return env, dt_range


def test_a_run_trades_and_writes_its_results(run_env):
    env, dt_range = run_env

    runner = Runner(env, dt_range, fee_rates=FEES, chunk_days=1)
    runner.run()

    # A run log per parameter set, holding the provenance of the numbers.
    run_logs = run_dirs(env)
    assert len(run_logs) == 1
    log = load(run_logs[0])
    assert log["strategy"] == "example.strategy.v1"
    assert log["params"] == {"fast_period": 20, "slow_period": 120}
    assert log["start_str"] == dt_range.start_str

    # The strategy traded, and the evaluation was run at all three slippage rates.
    result = log["result"]
    for suffix in ("0", "5", "10"):
        assert f"pnl{suffix}" in result
        assert f"sharpe_ratio_annualized{suffix}" in result
    assert result["pnl_cnt0"] >= 1, "the strategy closed no position; the test data is not forcing a trade"

    # The folder holds every closed trade, and each grouping adds up to the summary.
    folder = run_logs[0]
    assert folder.name == log["id"]
    assert "result" not in run_dir.read_json(folder, run_dir.META), "meta.json is the input, not the result"
    with open(folder / run_dir.TRADES) as f:
        trades = list(csv.DictReader(f))
    assert len(trades) == result["pnl_cnt0"]
    assert sum(float(t["pnl_change5"]) for t in trades) == pytest.approx(result["pnl5"])
    assert {t["side"] for t in trades} <= {"long", "short"}
    # Each close records its fill's price, which is what a results viewer charts.
    assert all(float(t["close_price"]) > 0 for t in trades)
    for name in GROUPINGS:
        rows = run_dir.read_json(folder, run_dir.grouping_file(name))
        assert sum(r["trades"] for r in rows.values()) == result["pnl_cnt0"], name
        assert sum(r["pnl10"] for r in rows.values()) == pytest.approx(result["pnl10"]), name

    # The trades fall in one JST month and day, so those groupings' drawdown is the run's.
    month = run_dir.read_json(folder, "by_month.json")
    assert list(month) == ["2026-01"]
    assert month["2026-01"]["dd5"] == pytest.approx(result["max_drawdown5"])
    assert list(run_dir.read_json(folder, "by_day.json")) == ["2026-01-02"]
    hours = run_dir.read_json(folder, "by_hour.json")
    assert len(hours) == 24 and hours["06"]["trades"] == result["pnl_cnt0"]   # the range is 06:00-06:59 JST

    # No duplicate whole metric stream is kept in the run folder. A strategy
    # that emits BAR metrics writes those separately to the data lake.
    assert not (Path(env.work_dir) / "result").exists()


class MemoryWriter:
    """An `InfluxdbWriter` that keeps its points."""

    def __init__(self, bucket: str):
        self.bucket = bucket
        self.points = []
        self.closed = False

    def add(self, point) -> None:
        self.points.append(point)

    def close(self) -> None:
        self.closed = True


class MemoryStore:
    """An `InfluxdbStore` with no server behind it."""

    def __init__(self):
        self.writers: list[MemoryWriter] = []

    def create_writer(
        self, bucket_name: str, param_set_id: str | None = None, **_
    ) -> MemoryWriter:
        writer = MemoryWriter(bucket_name)
        self.writers.append(writer)
        return writer


def test_a_run_without_influx_still_has_its_result(run_env):
    """InfluxDB is optional: the result is computed from the reporters every run has."""
    env, dt_range = run_env
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()

    log = load(run_dirs(env)[0])
    assert log["result"]["pnl_cnt0"] >= 1
    assert log["influx_bucket"] is None


def test_influx_records_orders_pnl_and_evaluations(run_env):
    env, dt_range = run_env
    store = MemoryStore()
    runner = Runner(env, dt_range, fee_rates=FEES, chunk_days=1, influx=store)
    runner.run()

    log = load(run_dirs(env)[0])
    [writer] = store.writers
    assert writer.closed
    # The env, then the grid defaults in declaration order, then the batch.
    assert log["param_set_id"] == f"{ENV_NAME}_20_120"
    assert writer.bucket == log["influx_bucket"] == f"backtest_{ENV_NAME}_20_120_{runner.batch_id}"

    by_measurement = {}
    for point in writer.points:
        by_measurement.setdefault(point._name, []).append(point)
    assert {"ORDER", "PNL", "EVAL"} <= set(by_measurement)
    # Skipped on purpose: they are per-snapshot and already in the bronze layer.
    assert "MARKET_PRICE" not in by_measurement
    assert "TRADE" not in by_measurement

    # Every closed position twice, aligned to its close and to its entry, and one
    # EVAL per slippage rate.
    closes = log["result"]["pnl_cnt0"]
    aligns = [p._tags.get("time_align") for p in by_measurement["PNL"]]
    assert aligns.count("close") == aligns.count("open") == closes
    assert len(by_measurement["EVAL"]) == 3 * closes

    # Nothing from the warmup period -- except the open-aligned copy of a position
    # opened during warmup and closed inside the range, which is a real trade.
    start_ms = int(dt_range.start_ts * 1000)
    early = [p for p in writer.points if p._time < start_ms]
    assert all(p._name == "PNL" and p._tags.get("time_align") == "open" for p in early)


def test_a_skipped_parameter_set_opens_no_bucket(run_env):
    """A resumed sweep must not touch the database for results it already has."""
    env, dt_range = run_env
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()

    store = MemoryStore()
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1, influx=store).run()
    assert store.writers == []


def test_a_second_identical_run_is_skipped(run_env):
    """The deterministic run-log id is what makes an interrupted sweep resumable."""
    env, dt_range = run_env

    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()
    [log_path] = run_dirs(env)
    first = load(log_path)

    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()
    # Same identity, so no second log, and the first was not rewritten.
    assert run_dirs(env) == [log_path]
    assert load(log_path)["start_ts"] == first["start_ts"]


def test_a_forced_rerun_writes_a_bucket_of_its_own_and_replaces_the_folder(run_env):
    """Re-running must not write on top of the earlier run's points; the folder is the run's id, so it is replaced."""
    env, dt_range = run_env
    first = Runner(env, dt_range, fee_rates=FEES, chunk_days=1, influx=MemoryStore())
    first.run()

    store = MemoryStore()
    second = Runner(env, dt_range, fee_rates=FEES, chunk_days=1, influx=store, force=True)
    second.run()

    [writer] = store.writers
    assert second.batch_id != first.batch_id
    assert writer.bucket == f"backtest_{ENV_NAME}_20_120_{second.batch_id}"
    [path] = run_dirs(env)
    assert load(path)["influx_bucket"] == writer.bucket


def test_force_reruns_a_completed_parameter_set(run_env):
    env, dt_range = run_env

    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()
    [log_path] = run_dirs(env)
    first = load(log_path)

    Runner(env, dt_range, fee_rates=FEES, chunk_days=1, force=True).run()
    assert load(log_path)["start_ts"] > first["start_ts"]


def weekly_sweep(env, dt_range, **kwargs) -> Runner:
    # The data is one day, so there is no day after it to overlap; see `test_weekly` for the overlap.
    runner = Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1, weekly=WeeklySettings(overlap_days=0), **kwargs)
    runner.run()
    return runner


def test_a_run_does_not_choose_and_select_chooses_from_weekly_runs_only(run_env):
    env, dt_range = run_env
    root = Path(env.work_dir)

    # A sweep without --weekly is a quick look: its results, and no recommendation.
    Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1).run()
    assert not (root / "best_result").exists(), "a run writes results; it does not choose"
    assert all(load(p)["is_temporary_run"] for p in run_dirs(env))
    assert best.recommend(root, 1, SelectionSettings()) == []

    runner = weekly_sweep(env, dt_range)
    combos = runner.param_sets()
    assert len(combos) == 6  # 3 fast periods x 2 slow periods
    weekly_folders = [p for p in run_dirs(env) if not load(p)["is_temporary_run"]]
    assert len(weekly_folders) == len(combos)

    groups = best.recommend(root, runner.batch_id, SelectionSettings())
    # Both parameters are numbers, so the sweep is one group: one recommendation.
    [group] = groups
    assert group["group"] == {} and group["runs"] == len(combos)
    # The week of 2026-01-02, clipped to the one day on disk.
    assert (group["start_str"], group["end_str"]) == ("2026-01-02 00:00:00", "2026-01-02 23:59:59")
    assert group["method"] == "whole range"   # a day holds no complete month

    # best_result/<batch_id>/ holds the selection, and each recommended run's meta.json.
    best_dir = root / "best_result" / str(runner.batch_id)
    selection_file = json.loads((best_dir / "selection.json").read_text())
    assert selection_file["groups"][0]["ranking"][0]["params"] == group["ranking"][0]["params"]
    copies = sorted(p.name for p in best_dir.iterdir() if p.name != "selection.json")
    if group["recommended"] is None:
        assert copies == [] and group["recommended_run_id"] is None
    else:
        run_id = group["recommended_run_id"]
        assert copies == [f"{run_id}.json"]
        assert load(run_dir.run_dir(env.work_dir, run_id))["params"] == group["recommended"]
        assert (best_dir / copies[0]).read_text() == (run_dir.run_dir(env.work_dir, run_id) / run_dir.META).read_text()

    # With every safeguard off, selection is the plain pnl5 ranking it replaced.
    plain = SelectionSettings(score="pnl5", positive_pnl10=False, neighbors=False, train_months=0)
    [plain_group] = best.recommend(root, runner.batch_id + 1, plain)
    chosen = load(run_dir.run_dir(env.work_dir, plain_group["recommended_run_id"]))
    assert chosen["result"]["pnl5"] == max(load(p)["result"]["pnl5"] for p in weekly_folders)


def test_a_sweep_in_several_batches_gets_the_same_results_as_one(run_env, tmp_path):
    """Between batches the data source is rewound while a chunk may be loading ahead."""
    env, dt_range = run_env

    Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1, max_concurrency=6).run()
    one_batch = {p.name: load(p)["result"] for p in run_dirs(env)}

    shutil.rmtree(Path(env.work_dir) / "run_log")
    Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1, max_concurrency=4).run()
    two_batches = {p.name: load(p)["result"] for p in run_dirs(env)}

    assert len(one_batch) == 6
    assert two_batches == one_batch


def test_a_backtest_that_raises_fails_without_stopping_the_others(run_env, monkeypatch):
    """One parameter set raises partway through the replay.

    qate's event loop catches the exception and carries on, so this used to
    produce a normal-looking result. Now that backtest stops at the first one,
    saves no run log, and marks the run failed -- and, having stopped early, it
    must release the others rather than leave them waiting at the barrier.
    """
    from example.strategy.v1 import Variant

    env, dt_range = run_env
    calls = {"n": 0}
    original = Variant.handle_order_book

    def flaky(self, order_book):
        if self.fast_period == 40:
            calls["n"] += 1
            if calls["n"] > 100:
                raise ValueError("the strategy broke")
        return original(self, order_book)

    monkeypatch.setattr(Variant, "handle_order_book", flaky)

    runner = Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1)
    runner.run()   # must return: a hang here is the deadlock

    logs = [load(p) for p in run_dirs(env)]
    assert len(logs) == 4, "the two fast_period=40 sets failed; the other four completed"
    assert all(log["params"]["fast_period"] != 40 for log in logs)
    assert sorted(p["slow_period"] for p in runner.failed) == [120, 240]
    assert all(p["fast_period"] == 40 for p in runner.failed)


def test_a_resumed_sweep_ranks_the_combinations_it_skipped(run_env):
    """The best of a resumed sweep must include results it did not just compute."""
    env, dt_range = run_env
    root = Path(env.work_dir)

    # A first sweep of everything, then a second that skips all of it.
    first = weekly_sweep(env, dt_range)
    [best_first] = best.recommend(root, first.batch_id, SelectionSettings())

    second = weekly_sweep(env, dt_range)
    groups = best.recommend(root, second.batch_id, SelectionSettings())

    assert groups, "a resumed sweep with nothing to run still has a best"
    [best_second] = groups
    assert best_second["runs"] == best_first["runs"] == 6
    assert best_second["recommended_run_id"] == best_first["recommended_run_id"]
    assert best_second["ranking"][0]["params"] == best_first["ranking"][0]["params"]


def test_a_backtest_never_reloads_parameters(run_env, monkeypatch):
    """Every single run and sweep member keeps the parameters it was built with."""
    from qate.trading.strategy import Strategy
    from qate.trading.trader import Trader

    env, dt_range = run_env
    monkeypatch.chdir(env.work_dir)

    future = time.time() + 3600
    monkeypatch.setattr(Trader, "tick_ts", property(lambda self: future))
    offered = []
    monkeypatch.setattr(Strategy, "update_params", lambda self, new: offered.append(new) or False)

    Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1).run()

    assert len(run_dirs(env)) == 6
    assert offered == [], "a backtest tried to reload a strategy's parameters"


def test_a_single_run_uses_the_grid_defaults(run_env):
    env, dt_range = run_env

    runner = Runner(env, dt_range, fee_rates=FEES, chunk_days=1)
    assert runner.param_sets() == [{"fast_period": 20, "slow_period": 120}]


def test_optimize_still_reads_the_grid(run_env):
    """Optimization expands every value in the same definition."""
    env, dt_range = run_env

    runner = Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1)
    combos = runner.param_sets()
    assert len(combos) == 6


def test_an_env_needs_no_desc_json(run_env):
    """The Config class comes from the strategy module. There is no index file."""
    env, dt_range = run_env
    assert not (Path(env.work_dir) / "desc.json").exists()

    runner = Runner(env, dt_range, fee_rates=FEES, chunk_days=1)
    runner.run()
    assert run_dirs(env)


def test_a_run_log_records_the_config_that_produced_it(run_env):
    """Provenance: a result you cannot attribute to a configuration is not a result."""
    env, dt_range = run_env
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()

    log = load(run_dirs(env)[0])
    assert log["env"] == str(ENV_NAME)
    assert log["config"]["market"] == "COINCHECK:BTC_SPOT"
    assert log["config"]["order_size"] == 0.002
    assert log["trading"]["variant_name"] == "v1"
    assert log["params"] == {"fast_period": 20, "slow_period": 120}


def test_a_range_with_no_data_is_reported_not_silently_empty(run_env, caplog):
    env, _ = run_env
    empty_range = DtRange.from_strings("20260201", "20260202")

    runner = Runner(env, empty_range, fee_rates=FEES, chunk_days=1)
    with caplog.at_level("WARNING"):
        runner.run()

    assert any("not on disk" in r.message for r in caplog.records)


def test_each_backtest_gets_its_own_skip_set():
    """One set read by every thread per event made a free-threaded sweep 2.4x slower."""
    from qate.core.model import ExchangeName
    from qate.trading.replay import ReplayQueue
    from qate.trading.strategy import Strategy

    from enoshima.backtest import BacktestRuntime
    from enoshima.influx import UNRECORDED_MEASUREMENTS

    a, b = Strategy(), Strategy()
    BacktestRuntime(a, [ExchangeName.GMO], ReplayQueue([]))
    BacktestRuntime(b, [ExchangeName.GMO], ReplayQueue([]))
    assert a.skip_measurements == b.skip_measurements == UNRECORDED_MEASUREMENTS
    assert a.skip_measurements is not b.skip_measurements
    assert a.skip_measurements is not UNRECORDED_MEASUREMENTS
