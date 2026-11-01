"""A real run, from bronze parquet in a data lake to its results in the same lake.

Writes a synthetic bronze tree, copies the example environment into a temporary
env root, and drives `Runner` exactly as `enoshima run` does. If this passes, the
documented quick start works.

The prices are a sine wave, so the fast and slow averages cross repeatedly and
the strategy actually opens and closes positions. A backtest that trades nothing
would satisfy a weaker test while telling us nothing, and getting this wrong is
easy: the first version of this file generated books that all fell before the
reported range began, so every one of them was warmup and nothing was recorded.
"""

import json
import math
import shutil
import time
from dataclasses import dataclass
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

from enoshima import lake
from enoshima.lake import Store
from enoshima.ledger import GROUPINGS
from enoshima.records import TAGS_KEY
from enoshima.runner import WARMUP_HOURS, Runner
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


def store_of(env) -> Store:
    return Store.of(str(env.name))


@dataclass(frozen=True)
class RunRef:
    """A finished run in the lake, named as its folder once was."""

    store: Store
    name: str


def run_dirs(env) -> list[RunRef]:
    """Every finished run of the env, in the lake."""
    store = store_of(env)
    return [RunRef(store, run_id) for run_id in store.run_ids()]


def load(ref: RunRef) -> dict:
    """A run as one dict: its meta, with its summary as `result`."""
    run = ref.store.read_run(ref.name)
    return {**run["meta"], "result": run["summary"]}


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

    # The lake holds every closed trade, and each grouping adds up to the summary.
    ref = run_logs[0]
    store = ref.store
    assert ref.name == log["id"]
    assert "result" not in store.read_run(ref.name)["meta"], "the meta is the input, not the result"
    assert store.parts(ref.name) == [lake.WHOLE]
    trades = store.trades(ref.name)
    assert len(trades) == result["pnl_cnt0"]
    assert sum(t.pnl_change5 for t in trades) == pytest.approx(result["pnl5"])
    assert {t.side for t in trades} <= {"long", "short"}
    # Each close records its fill's price, which is what a results viewer charts.
    assert all(t.close_price > 0 for t in trades)
    groupings = {name: store.grouping(ref.name, name) for name in GROUPINGS}
    for name in GROUPINGS:
        rows = groupings[name]
        assert sum(r["trades"] for r in rows.values()) == result["pnl_cnt0"], name
        assert sum(r["pnl10"] for r in rows.values()) == pytest.approx(result["pnl10"]), name

    # The trades fall in one JST month and day, so those groupings' drawdown is the run's.
    month = groupings["month"]
    assert list(month) == ["2026-01"]
    assert month["2026-01"]["dd5"] == pytest.approx(result["max_drawdown5"])
    assert list(groupings["day"]) == ["2026-01-02"]
    hours = groupings["hour"]
    assert len(hours) == 24 and hours["06"]["trades"] == result["pnl_cnt0"]   # the range is 06:00-06:59 JST

    # Nothing is written to the environment directory: results are the lake's.
    assert sorted(p.name for p in Path(env.work_dir).iterdir() if p.name not in ("env.log", ".lock")) == sorted(
        p.name for p in (EXAMPLE_ROOT / "env" / ENV_NAME).iterdir())


def test_a_run_records_orders_pnl_and_evaluations_in_the_lake(run_env, data_root):
    env, dt_range = run_env
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()

    [ref] = run_dirs(env)
    log = load(ref)
    # The env, then the grid defaults in declaration order.
    assert log["param_set_id"] == f"{ENV_NAME}_20_120"
    assert "influx_bucket" not in log

    # Under the lake's research layer, partitioned by env, run id and part.
    orders = ref.store.path(lake.ORDER, ref.name, lake.WHOLE)
    assert orders == (data_root / "research" / "dataset=BacktestOrder" / f"env={ENV_NAME}" / f"run_id={ref.name}"
                      / "part=all" / "data.parquet")
    assert orders.is_file()

    closes = log["result"]["pnl_cnt0"]
    pnl = ref.store.records(lake.PNL, ref.name).to_pylist()
    assert sum(1 for r in pnl if r["SETTLE_TYPE"] == "CLOSE") == closes
    assert {r["EXCHANGE_NAME"] for r in pnl} == {"COINCHECK"}, "tags are written by name"
    assert all(r["open_ts"] is not None for r in pnl if r["SETTLE_TYPE"] == "CLOSE")
    # The file says which columns were tags.
    tags = pq.ParquetFile(ref.store.path(lake.PNL, ref.name, lake.WHOLE)).schema_arrow.metadata[TAGS_KEY]
    assert json.loads(tags) == ["EXCHANGE_NAME", "SETTLE_TYPE", "SIDE", "SYMBOL"]
    metrics = ref.store.records(lake.METRIC, ref.name).to_pylist()
    # One EVAL per close and slippage rate; the per-event measurements are bronze's, never recorded.
    assert sum(1 for m in metrics if m["measurement"] == "EVAL") == 3 * closes
    assert not {"MARKET_PRICE", "TRADE"} & {m["measurement"] for m in metrics}

    # Nothing from the warmup period, except a close in range of a position opened in warmup.
    order_rows = ref.store.records(lake.ORDER, ref.name).to_pylist()
    assert {"EXEC_PRICE", "SIDE", "SETTLE_TYPE"} <= set(order_rows[0])
    assert all(r["ts"] >= dt_range.start_ts for r in order_rows + pnl)


def test_the_run_row_is_a_queryable_view_of_meta_and_summary(run_env, data_root):
    env, dt_range = run_env
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()
    [ref] = run_dirs(env)

    [path] = (data_root / "research" / "dataset=BacktestRun").rglob("data.parquet")
    row = pq.ParquetFile(path).read().to_pylist()[0]
    assert row["strategy"] == "example.strategy.v1" and row["param_set_id"] == f"{ENV_NAME}_20_120"
    assert row["strategy_rev"] == load(ref)["strategy_rev"]
    assert json.loads(row["params"]) == {"fast_period": 20, "slow_period": 120}
    assert row["pnl5"] == pytest.approx(load(ref)["result"]["pnl5"])
    assert "env" in row and "run_id" not in row, "the partitions are the path's, not the file's"


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


def test_a_forced_rerun_replaces_the_runs_files(run_env):
    """The files are the run's id, so a re-run replaces them rather than adding a second copy."""
    env, dt_range = run_env
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()
    [ref] = run_dirs(env)
    orders = ref.store.records(lake.ORDER, ref.name).num_rows

    second = Runner(env, dt_range, fee_rates=FEES, chunk_days=1, force=True)
    second.run()
    assert run_dirs(env) == [ref]
    assert load(ref)["batch_id"] == second.batch_id
    assert ref.store.records(lake.ORDER, ref.name).num_rows == orders


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


def test_a_run_writes_its_results_and_chooses_nothing(run_env):
    env, dt_range = run_env
    root = store_of(env)

    # A sweep without --weekly is a quick look: temporary runs, nothing else.
    Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1).run()
    assert all(load(p)["is_temporary_run"] for p in run_dirs(env))
    # Only the Backtest datasets: no recommendation, no analysis folder.
    assert sorted(p.name for p in (root.root / lake.LAYER).iterdir()) == sorted(
        f"dataset={d}" for d in lake.DATASETS if (root.root / lake.LAYER / f"dataset={d}").is_dir())

    runner = weekly_sweep(env, dt_range)
    combos = runner.param_sets()
    assert len(combos) == 6  # 3 fast periods x 2 slow periods
    weekly_runs = [p for p in run_dirs(env) if not load(p)["is_temporary_run"]]
    assert len(weekly_runs) == len(combos)
    # The week of 2026-01-02, clipped to the one day on disk.
    assert {(load(p)["start_str"], load(p)["end_str"]) for p in weekly_runs} == {
        ("2026-01-02 00:00:00", "2026-01-02 23:59:59")}


def test_a_sweep_in_several_batches_gets_the_same_results_as_one(run_env, tmp_path):
    """Between batches the data source is rewound while a chunk may be loading ahead."""
    env, dt_range = run_env

    Runner(env, dt_range, optimize="grid", fee_rates=FEES, chunk_days=1, max_concurrency=6).run()
    one_batch = {p.name: load(p)["result"] for p in run_dirs(env)}

    for ref in run_dirs(env):
        ref.store.delete_run(ref.name)
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


def test_a_resumed_sweep_keeps_the_runs_it_skipped(run_env):
    """A sweep run again finds every combination in the lake, under the same ids, and replays none."""
    env, dt_range = run_env

    weekly_sweep(env, dt_range)
    first = {p.name: load(p)["result"] for p in run_dirs(env)}
    weekly_sweep(env, dt_range)
    second = {p.name: load(p)["result"] for p in run_dirs(env)}

    assert len(first) == 6 and second == first


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
    from qate.core.ev_q import EventQueue
    from qate.core.model import ExchangeName
    from qate.trading.strategy import Strategy

    from enoshima.backtest import BacktestRuntime
    from enoshima.records import UNRECORDED_MEASUREMENTS

    a, b = Strategy(), Strategy()
    BacktestRuntime(a, [ExchangeName.GMO], EventQueue())
    BacktestRuntime(b, [ExchangeName.GMO], EventQueue())
    assert a.skip_measurements == b.skip_measurements == UNRECORDED_MEASUREMENTS
    assert a.skip_measurements is not b.skip_measurements
    assert a.skip_measurements is not UNRECORDED_MEASUREMENTS
