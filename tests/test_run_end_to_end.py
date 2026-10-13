"""A real run, from parquet on disk to a run log on disk.

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

import json
import math
import shutil
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hase.bronze import DataType
from hase.layout import data_path
from qate.core.model import ExchangeName, Market
from qate.core.symbol import Symbol
from qate.util.dt_range import DtRange
from qate_env.env import Env
from qate_env.env_name import EnvName

from enoshima.runner import WARMUP_HOURS, Runner

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent / "example"
ENV_NAME = EnvName("EXAMPLE_BACKTEST")
MARKET = Market(ExchangeName.COINCHECK, Symbol.BTC_SPOT)
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

    runner = Runner(env, dt_range, chunk_days=1)
    runner.run()

    # A run log per parameter set, holding the provenance of the numbers.
    run_logs = list((Path(env.work_dir) / "run_log").glob("*.json"))
    assert len(run_logs) == 1
    log = json.loads(run_logs[0].read_text())
    assert log["strategy"] == "example.strategy.v1"
    assert log["params"] == {"fast_period": 20, "slow_period": 120}
    assert log["start_str"] == dt_range.start_str

    # The strategy traded, and the evaluation was run at all three slippage rates.
    result = log["result"]
    for suffix in ("0", "5", "10"):
        assert f"pnl{suffix}" in result
        assert f"sharpe_ratio_annualized{suffix}" in result
    assert result["pnl_cnt0"] >= 1, "the strategy closed no position; the test data is not forcing a trade"

    # Trading data goes to InfluxDB or nowhere; a run writes no local copy.
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

    def create_writer(self, bucket_name: str, param_set_id: str | None = None, **_) -> MemoryWriter:
        writer = MemoryWriter(bucket_name)
        self.writers.append(writer)
        return writer


def test_a_run_without_influx_still_has_its_result(run_env):
    """InfluxDB is optional: the result is computed from the reporters every run has."""
    env, dt_range = run_env
    Runner(env, dt_range, chunk_days=1).run()

    log = json.loads(next((Path(env.work_dir) / "run_log").glob("*.json")).read_text())
    assert log["result"]["pnl_cnt0"] >= 1
    assert log["influx_bucket"] is None


def test_influx_records_orders_pnl_and_evaluations(run_env):
    env, dt_range = run_env
    store = MemoryStore()
    Runner(env, dt_range, chunk_days=1, influx=store).run()

    log = json.loads(next((Path(env.work_dir) / "run_log").glob("*.json")).read_text())
    [writer] = store.writers
    assert writer.closed
    assert writer.bucket == log["influx_bucket"] == f"backtest_{ENV_NAME}_{log['param_set_id']}"

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
    Runner(env, dt_range, chunk_days=1).run()

    store = MemoryStore()
    Runner(env, dt_range, chunk_days=1, influx=store).run()
    assert store.writers == []


def test_a_second_identical_run_is_skipped(run_env):
    """The deterministic run-log id is what makes an interrupted sweep resumable."""
    env, dt_range = run_env

    Runner(env, dt_range, chunk_days=1).run()
    [log_path] = (Path(env.work_dir) / "run_log").glob("*.json")
    first = json.loads(log_path.read_text())

    Runner(env, dt_range, chunk_days=1).run()
    # Same identity, so no second log, and the first was not rewritten.
    assert list((Path(env.work_dir) / "run_log").glob("*.json")) == [log_path]
    assert json.loads(log_path.read_text())["start_ts"] == first["start_ts"]


def test_force_reruns_a_completed_parameter_set(run_env):
    env, dt_range = run_env

    Runner(env, dt_range, chunk_days=1).run()
    [log_path] = (Path(env.work_dir) / "run_log").glob("*.json")
    first = json.loads(log_path.read_text())

    Runner(env, dt_range, chunk_days=1, force=True).run()
    assert json.loads(log_path.read_text())["start_ts"] > first["start_ts"]


def test_optimize_grid_runs_every_combination_and_picks_a_best(run_env):
    env, dt_range = run_env

    runner = Runner(env, dt_range, optimize="grid", chunk_days=1)
    combos = runner.param_sets()
    assert len(combos) == 6  # 3 fast periods x 2 slow periods

    runner.run()
    best = runner.post_analysis()
    assert best is not None
    assert "params" in best and "result" in best

    # One run log per combination, and the best is one of them.
    logs = list((Path(env.work_dir) / "run_log").glob("*.json"))
    assert len(logs) == len(combos)
    all_pnl = [json.loads(p.read_text())["result"]["pnl5"] for p in logs]
    assert best["result"]["pnl5"] == max(all_pnl)

    best_files = list((Path(env.work_dir) / "best_result").glob("*.json"))
    assert len(best_files) == 1


def test_a_resumed_sweep_ranks_the_combinations_it_skipped(run_env):
    """The best of a resumed sweep must include results it did not just compute."""
    env, dt_range = run_env

    # A first sweep of everything, then a second that skips all of it.
    first = Runner(env, dt_range, optimize="grid", chunk_days=1)
    first.run()
    best_first = first.post_analysis()

    second = Runner(env, dt_range, optimize="grid", chunk_days=1)
    second.run()
    best_second = second.post_analysis()

    assert best_second is not None, "a resumed sweep with nothing to run still has a best"
    assert best_second["result"]["pnl5"] == best_first["result"]["pnl5"]
    assert best_second["params"] == best_first["params"]


def test_a_single_run_uses_params_json_not_the_grid_defaults(run_env):
    """The same file a live run reads, so "backtest what prod runs" is one command."""
    env, dt_range = run_env

    # Make the two sources disagree, then check which one won.
    params_path = Path(env.work_dir) / "params.json"
    params_path.write_text(json.dumps({"fast_period": 30, "slow_period": 90}))

    runner = Runner(env, dt_range, chunk_days=1)
    assert runner.param_sets() == [{"fast_period": 30, "slow_period": 90}]

    runner.run()
    log = json.loads(next((Path(env.work_dir) / "run_log").glob("*.json")).read_text())
    assert log["params"] == {"fast_period": 30, "slow_period": 90}


def test_an_empty_params_json_falls_back_to_the_grid_defaults(run_env):
    """Backtest envs written before this left params.json empty; do not just refuse."""
    env, dt_range = run_env
    (Path(env.work_dir) / "params.json").write_text("{}")

    runner = Runner(env, dt_range, chunk_days=1)
    assert runner.param_sets() == [{"fast_period": 20, "slow_period": 120}]


def test_optimize_still_reads_the_grid(run_env):
    """A grid is a search space, and only --optimize consults it."""
    env, dt_range = run_env
    (Path(env.work_dir) / "params.json").write_text(json.dumps({"fast_period": 30, "slow_period": 90}))

    runner = Runner(env, dt_range, optimize="grid", chunk_days=1)
    combos = runner.param_sets()
    assert len(combos) == 6
    assert {"fast_period": 30, "slow_period": 90} not in combos


def test_an_env_needs_no_desc_json(run_env):
    """The Config class comes from the strategy module. There is no index file."""
    env, dt_range = run_env
    assert not (Path(env.work_dir) / "desc.json").exists()

    runner = Runner(env, dt_range, chunk_days=1)
    runner.run()
    assert list((Path(env.work_dir) / "run_log").glob("*.json"))


def test_a_run_log_records_the_config_that_produced_it(run_env):
    """Provenance: a result you cannot attribute to a configuration is not a result."""
    env, dt_range = run_env
    Runner(env, dt_range, chunk_days=1).run()

    log = json.loads(next((Path(env.work_dir) / "run_log").glob("*.json")).read_text())
    assert log["env"] == str(ENV_NAME)
    assert log["config"]["market"] == "COINCHECK:BTC_SPOT"
    assert log["config"]["order_size"] == 0.002
    assert log["trading"]["variant"] == "v1"
    assert log["params"] == {"fast_period": 20, "slow_period": 120}


def test_a_range_with_no_data_is_reported_not_silently_empty(run_env, caplog):
    env, _ = run_env
    empty_range = DtRange.from_strings("20260201", "20260202")

    runner = Runner(env, empty_range, chunk_days=1)
    with caplog.at_level("WARNING"):
        runner.run()

    assert any("not on disk" in r.message for r in caplog.records)
