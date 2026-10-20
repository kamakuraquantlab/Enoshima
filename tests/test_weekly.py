"""Strict weekly runs: weeks computed once, and any window of them the same as one run of it.

The data is a sine wave of books, one a minute, over three ISO weeks and the
day before them, so the example strategy trades through every week and across
every Monday. See `test_run_end_to_end` for the shape of a run.
"""

import datetime as dt
import math
import shutil
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hase.bronze import DataType
from hase.layout import data_path
from qate.util.dt_range import DtRange
from qate_env.env import Env
from test_run_end_to_end import ENV_NAME, EXAMPLE_ROOT, MARKET, MemoryStore

from enoshima import run_dir, weekly
from enoshima.eval import SLIPPAGE_05, SLIPPAGE_10
from enoshima.ledger import JST
from enoshima.runner import Runner
from enoshima.weekly import Week, WeeklySettings

DEPTH = 3
SPREAD = 1000.0
STEP_SECONDS = 60
# 600 books a cycle against a 120-book slow average: a crossing every few hours.
CYCLE_BOOKS = 600
AMPLITUDE = 100_000.0
BASE_PRICE = 15_000_000.0

FIRST_DAY = dt.date(2026, 1, 4)  # the Sunday before 2026-W02: the preload
LAST_DAY = dt.date(2026, 1, 25)  # the Sunday ending 2026-W04
W02, W03, W04 = Week.parse("2026-W02"), Week.parse("2026-W03"), Week.parse("2026-W04")


def write_days(root: Path) -> None:
    """One parquet file per JST day, as bronze partitions them."""
    columns = ["ts"] + [f"{side}{i}_{x}" for side in ("bid", "ask") for i in range(DEPTH) for x in ("price", "qty")]
    origin = dt.datetime.combine(FIRST_DAY, dt.time(), JST).timestamp()
    day = FIRST_DAY
    n = 0
    while day <= LAST_DAY:
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), JST).timestamp()
        rows = []
        while origin + n * STEP_SECONDS < end:
            mid = BASE_PRICE + AMPLITUDE * math.sin(2 * math.pi * n / CYCLE_BOOKS)
            row = {"ts": origin + n * STEP_SECONDS}
            for i in range(DEPTH):
                row[f"bid{i}_price"] = mid - SPREAD / 2 - i * 100
                row[f"bid{i}_qty"] = 1.0
                row[f"ask{i}_price"] = mid + SPREAD / 2 + i * 100
                row[f"ask{i}_qty"] = 1.0
            rows.append(row)
            n += 1
        path = data_path(root, MARKET.id, DataType.ORDER_BOOK, day.isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows, columns=columns), preserve_index=False), path)
        day += dt.timedelta(days=1)


@pytest.fixture
def make_env(tmp_path, data_root, monkeypatch):
    """A function that copies the example environment to a fresh env root; the data is shared."""
    write_days(data_root)
    monkeypatch.chdir(tmp_path)

    def make(name: str) -> Env:
        env_root = tmp_path / name
        shutil.copytree(EXAMPLE_ROOT / "env", env_root)
        return Env(str(env_root), ENV_NAME)

    return make


def weeks_range(first: Week, last: Week) -> DtRange:
    return DtRange(first.start, last.dt_range.end_dt)


def run(env: Env, dt_range: DtRange, **kwargs) -> Runner:
    runner = Runner(env, dt_range, chunk_days=2, weekly=WeeklySettings(), **kwargs)
    runner.run()
    assert not runner.failed
    return runner


def the_run(env: Env) -> Path:
    [path] = (Path(env.work_dir) / run_dir.RUN_LOG_DIR).iterdir()
    return path


# ---------------------------------------------------------------- the calendar


def test_a_week_is_monday_to_sunday_in_jst():
    [week] = weekly.covering(DtRange.from_strings("2026-10-08"))
    assert week.key == "2026-W41"
    assert week.dt_range.start_str == "2026-10-05 00:00:00"
    assert week.dt_range.end_str == "2026-10-11 23:59:59"
    assert week.replay_range(24).start_str == "2026-10-04 00:00:00"
    assert week.replay_range(24).days == [f"2026-10-{d:02d}" for d in range(4, 12)]


def test_the_covering_weeks_are_the_fewest_that_contain_the_range():
    weeks = weekly.covering(DtRange.from_strings("2026-12-31", "2027-01-11"))
    assert [w.key for w in weeks] == ["2026-W53", "2027-W01", "2027-W02"]
    assert [Week.parse(w.key) for w in weeks] == weeks
    # A Sunday is the end of its week, a Monday the start of the next.
    assert [w.key for w in weekly.covering(DtRange.from_strings("2026-10-11", "2026-10-12"))] == [
        "2026-W41",
        "2026-W42",
    ]


def test_preload_hours_is_checked():
    for bad in (-1, 1.5, True, "24"):
        with pytest.raises(ValueError):
            WeeklySettings(preload_hours=bad)


# ---------------------------------------------------------------- the runs


def test_a_range_is_widened_to_its_weeks(make_env):
    env = make_env("env")
    runner = run(env, DtRange.from_strings("2026-01-07", "2026-01-08"))
    assert runner.weeks == [W02]
    assert (runner.dt_range.start_str, runner.dt_range.end_str) == ("2026-01-05 00:00:00", "2026-01-11 23:59:59")

    folder = the_run(env)
    assert sorted(p.name for p in (folder / run_dir.WEEKS_DIR).iterdir()) == ["2026-W02"]
    meta = run_dir.read_json(folder, run_dir.META)
    assert (meta["mode"], meta["preload_hours"], meta["weeks"]) == ("weekly", 24, 1)
    assert "batch_id" not in meta and "week" not in meta, "the root holds what every week shares"
    week_meta = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, "2026-W02"), run_dir.META)
    assert (week_meta["week"], week_meta["start_str"]) == ("2026-W02", "2026-01-05 00:00:00")
    assert week_meta["batch_id"] == runner.batch_id


def test_the_root_is_rebuilt_from_every_week_on_disk(make_env):
    """Not the current run's result: the weeks', over the first Monday to the last Sunday."""
    env = make_env("env")
    run(env, weeks_range(W02, W03))
    run(env, weeks_range(W04, W04))
    folder = the_run(env)

    meta = run_dir.read_json(folder, run_dir.META)
    assert (meta["start_str"], meta["end_str"], meta["weeks"]) == ("2026-01-05 00:00:00", "2026-01-25 23:59:59", 3)
    trades, summary = weekly.window(env.work_dir, folder.name, [W02, W03, W04])
    assert run_dir.read_json(folder, run_dir.SUMMARY) == json_round_trip(summary)
    assert run_dir.read_trades(folder) == trades
    assert sum(r["trades"] for r in run_dir.read_json(folder, "by_week.json").values()) == len(trades)
    # An ordinary run folder to everything that reads them.
    assert list(run_dir.iter_complete(Path(env.work_dir))) == [folder]

    # Asking again for an earlier week alone runs nothing and keeps the whole range.
    run(env, weeks_range(W02, W02))
    assert run_dir.read_json(folder, run_dir.META)["end_str"] == "2026-01-25 23:59:59"


def json_round_trip(data):
    import json

    from qate.util.encoder import Encoder

    return json.loads(json.dumps(data, cls=Encoder))


def test_a_gap_between_the_weeks_on_disk_and_those_asked_for_is_filled(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    runner = run(env, weeks_range(W04, W04))
    folder = the_run(env)
    assert [w.key for w in weekly.on_disk(env.work_dir, folder.name)] == ["2026-W02", "2026-W03", "2026-W04"]
    w03 = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, W03.key), run_dir.META)
    assert w03["batch_id"] == runner.batch_id
    assert run_dir.read_json(folder, run_dir.META)["weeks"] == 3


def test_a_root_whose_weeks_have_a_gap_is_not_complete_until_filled(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W04))
    folder = the_run(env)
    shutil.rmtree(run_dir.week_dir(env.work_dir, folder.name, W03.key))   # as if W03 had failed

    assert not weekly.rebuild(env.work_dir, folder.name, run_dir.read_json(folder, run_dir.META))
    assert not run_dir.is_complete(folder), "a summary over W02..W04 would claim a week it does not have"
    assert list(run_dir.iter_complete(Path(env.work_dir))) == []

    run(env, weeks_range(W04, W04))
    assert run_dir.is_complete(folder)
    assert run_dir.read_json(folder, run_dir.META)["weeks"] == 3


def test_a_weekly_sweep_is_selected_from_like_any_run(make_env):
    from enoshima import best
    from enoshima.selection import SelectionSettings

    env = make_env("env")
    runner = Runner(env, weeks_range(W02, W03), optimize="grid", chunk_days=2, weekly=WeeklySettings())
    runner.run()
    assert not runner.failed
    [group] = best.recommend(Path(env.work_dir), runner.batch_id, SelectionSettings())
    assert group["runs"] == 6
    assert (group["start_str"], group["end_str"]) == ("2026-01-05 00:00:00", "2026-01-18 23:59:59")


def test_the_run_id_has_no_dates_but_has_the_preload(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    first = the_run(env).name
    run(env, weeks_range(W03, W03))
    assert the_run(env).name == first, "a later week goes in the same folder"

    Runner(env, weeks_range(W02, W02), chunk_days=2, weekly=WeeklySettings(preload_hours=12)).run()
    assert len(list((Path(env.work_dir) / run_dir.RUN_LOG_DIR).iterdir())) == 2


def test_a_new_strategy_revision_is_a_new_folder(make_env, monkeypatch):
    """Weeks of two versions of the strategy are never put together."""
    import enoshima.runner

    env = make_env("env")
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module: "aaaaaaaaaaaa")
    run(env, weeks_range(W02, W02))
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module: "bbbbbbbbbbbb")
    run(env, weeks_range(W03, W03))

    folders = sorted((Path(env.work_dir) / run_dir.RUN_LOG_DIR).iterdir())
    assert len(folders) == 2
    revs = {run_dir.read_json(f, run_dir.META)["strategy_rev"]: [w.key for w in weekly.on_disk(env.work_dir, f.name)]
            for f in folders}
    assert revs == {"aaaaaaaaaaaa": ["2026-W02"], "bbbbbbbbbbbb": ["2026-W03"]}


def test_an_extended_window_replays_only_its_new_week_and_equals_one_run(make_env):
    """The requirement: weeks 2-3, then 2-4, gives byte for byte what 2-4 at once gives."""
    stepwise = make_env("stepwise")
    first = run(stepwise, weeks_range(W02, W03))
    second = run(stepwise, weeks_range(W02, W04))

    at_once = make_env("at_once")
    run(at_once, weeks_range(W02, W04))

    a, b = the_run(stepwise), the_run(at_once)
    assert a.name == b.name
    for week in (W02, W03, W04):
        wa = run_dir.week_dir(stepwise.work_dir, a.name, week.key)
        wb = run_dir.week_dir(at_once.work_dir, b.name, week.key)
        for name in (run_dir.TRADES, run_dir.SUMMARY):
            assert (wa / name).read_bytes() == (wb / name).read_bytes(), f"{week.key} {name}"
        # The second run computed W04 only; the first two weeks are the first run's.
        batch = run_dir.read_json(wa, run_dir.META)["batch_id"]
        assert batch == (second.batch_id if week == W04 else first.batch_id)

    trades, summary = weekly.window(stepwise.work_dir, a.name, [W02, W03, W04])
    assert summary == weekly.window(at_once.work_dir, b.name, [W02, W03, W04])[1]
    assert summary["pnl_cnt0"] >= 9, "the strategy must trade in every week for this to test anything"
    weeks = [run_dir.read_json(run_dir.week_dir(stepwise.work_dir, a.name, w.key), run_dir.SUMMARY)
             for w in (W02, W03, W04)]
    assert all(w["pnl_cnt0"] > 0 for w in weeks)
    assert summary["pnl_cnt0"] == sum(w["pnl_cnt0"] for w in weeks) == len(trades)
    assert summary["pnl5"] == pytest.approx(sum(w["pnl5"] for w in weeks))
    assert all("open_at_end" in w for w in weeks)


def test_a_window_equals_one_continuous_replay_when_the_preload_holds_the_state(make_env):
    """Not guaranteed in general -- see `weekly` -- but true of a strategy whose state fits in the preload.

    The example's slowest average is 120 books, two hours here, and the 24-hour
    preload re-creates a position open across a Monday. If this breaks, a week
    started cold has stopped matching the same week inside a longer run.
    """
    env = make_env("weekly")
    run(env, weeks_range(W02, W04))
    stitched, summary = weekly.window(env.work_dir, the_run(env).name, [W02, W03, W04])

    continuous_env = make_env("continuous")
    Runner(continuous_env, weeks_range(W02, W04), chunk_days=2).run()
    [path] = run_dir.iter_complete(Path(continuous_env.work_dir))
    continuous = run_dir.read_trades(path)

    assert any(t.open_ts < W03.start.timestamp() <= t.close_ts for t in stitched), "no trade crosses a Monday"
    assert [(t.close_ts, t.open_ts) for t in stitched] == [(t.close_ts, t.open_ts) for t in continuous]
    for a, b in zip(stitched, continuous):
        assert a.pnl_change5 == pytest.approx(b.pnl_change5)
    assert summary["pnl5"] == pytest.approx(run_dir.read_json(path, run_dir.SUMMARY)["pnl5"])


def test_a_window_of_one_week_is_that_weeks_summary(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    path = run_dir.week_dir(env.work_dir, the_run(env).name, W02.key)
    stored = run_dir.read_json(path, run_dir.SUMMARY)
    _, summary = weekly.window(env.work_dir, the_run(env).name, [W02])
    assert summary == {k: v for k, v in stored.items() if not k.startswith("order_") and k != "open_at_end"}


def test_a_window_with_a_week_missing_is_refused(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    with pytest.raises(ValueError, match="2026-W03"):
        weekly.window(env.work_dir, the_run(env).name, [W02, W03])


def test_a_trade_carries_both_legs_extra_slippage(make_env):
    """Charged at the close, so a position opened in the preload costs what any other does."""
    env = make_env("env")
    run(env, weeks_range(W02, W03))
    trades, _ = weekly.window(env.work_dir, the_run(env).name, [W02, W03])
    boundary = [t for t in trades if t.open_ts < W03.start.timestamp() <= t.close_ts]
    for t in trades:
        legs = t.open_cost + t.notional
        assert t.pnl_change0 - t.pnl_change5 == pytest.approx(legs * SLIPPAGE_05)
        assert t.pnl_change0 - t.pnl_change10 == pytest.approx(legs * SLIPPAGE_10)
    assert all(t.pnl_change0 == pytest.approx(t.pnl - t.fee) for t in boundary)


def test_summarize_reproduces_the_evaluations(make_env, monkeypatch):
    """`summarize` is how a window's figures are computed; it must agree with `Evaluation`."""
    env = make_env("env")
    captured = []
    original = Runner._save

    def save(self, backtest):
        captured.append(backtest.result)
        original(self, backtest)

    monkeypatch.setattr(Runner, "_save", save)
    Runner(env, DtRange.from_strings("2026-01-05", "2026-01-09"), chunk_days=2).run()
    [reporter] = captured
    expected = reporter.get_result()
    actual = weekly.summarize(reporter.trades, reporter.order_stats())
    assert expected["pnl_cnt0"] > 0
    assert set(actual) == set(expected)
    for key, value in expected.items():
        assert actual[key] == pytest.approx(value), key


def test_a_skipped_week_is_not_rerun_and_force_reruns_it(make_env):
    env = make_env("env")
    first = run(env, weeks_range(W02, W02))
    run(env, weeks_range(W02, W02))
    path = run_dir.week_dir(env.work_dir, the_run(env).name, W02.key)
    assert run_dir.read_json(path, run_dir.META)["batch_id"] == first.batch_id

    forced = run(env, weeks_range(W02, W02), force=True)
    assert run_dir.read_json(path, run_dir.META)["batch_id"] == forced.batch_id


# ---------------------------------------------------------------- InfluxDB


class DeletingStore(MemoryStore):
    """A `MemoryStore` that records the deletes asked of it, and applies them."""

    def __init__(self):
        super().__init__()
        self.deletes: list[tuple[str, dict]] = []

    def create_writer(self, bucket_name: str, param_set_id: str | None = None, tags: dict | None = None, **_):
        writer = super().create_writer(bucket_name, param_set_id)
        writer.tags = dict(tags or {})
        return writer

    def delete_tagged(self, bucket_name: str, tags: dict) -> None:
        self.deletes.append((bucket_name, tags))


def test_a_weekly_run_has_one_bucket_and_clears_a_week_before_writing_it(make_env):
    env = make_env("env")
    store = DeletingStore()
    run(env, weeks_range(W02, W03), influx=store)
    run_id = the_run(env).name

    bucket = f"backtest_{ENV_NAME}_20_120_w{run_id}"
    assert [w.bucket for w in store.writers] == [bucket, bucket]
    assert [w.tags for w in store.writers] == [{"week": "2026-W02"}, {"week": "2026-W03"}]
    assert store.deletes == [(bucket, {"week": "2026-W02"}), (bucket, {"week": "2026-W03"})]
    for week in (W02, W03):
        assert run_dir.read_json(run_dir.week_dir(env.work_dir, run_id, week.key), run_dir.META)["influx_bucket"] == bucket

    # A week already on disk opens nothing; a forced one is cleared and written again.
    again = DeletingStore()
    run(env, weeks_range(W02, W03), influx=again)
    assert again.writers == [] and again.deletes == []
    forced = DeletingStore()
    run(env, weeks_range(W03, W03), influx=forced, force=True)
    assert forced.deletes == [(bucket, {"week": "2026-W03"})]


def test_the_writer_tags_every_point():
    from influxdb_client import Point

    from enoshima.influx import InfluxdbWriter

    writer = InfluxdbWriter.__new__(InfluxdbWriter)
    writer.tags = {"week": "2026-W02", "param_set_id": "X"}
    writer._spread_timestamps = lambda points: None
    written = []
    writer.bucket_name = "b"
    writer.w = type("W", (), {"write": lambda self, bucket, record, write_precision: written.extend(record)})()
    writer.write([Point("PNL").field("v", 1.0)])
    assert written[0]._tags == {"week": "2026-W02", "param_set_id": "X"}


# ---------------------------------------------------------------- as far as the data goes


def remove_days(data_root: Path, first: dt.date, last: dt.date) -> None:
    day = first
    while day <= last:
        data_path(data_root, MARKET.id, DataType.ORDER_BOOK, day.isoformat()).unlink()
        day += dt.timedelta(days=1)


def test_a_week_the_data_does_not_reach_is_not_run(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, Week.parse("2026-W06")))
    folder = the_run(env)
    assert [w.key for w in weekly.on_disk(env.work_dir, folder.name)] == ["2026-W02", "2026-W03", "2026-W04"]
    assert run_dir.read_json(folder, run_dir.META)["end_str"] == "2026-01-25 23:59:59"


def test_a_last_week_is_run_as_far_as_the_data_goes_and_grows_when_it_arrives(make_env, data_root):
    env = make_env("env")
    remove_days(data_root, dt.date(2026, 1, 22), LAST_DAY)   # W04 has Monday to Wednesday
    first = run(env, weeks_range(W02, W04))
    folder = the_run(env)

    w04 = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, W04.key), run_dir.META)
    assert (w04["start_str"], w04["end_str"], w04["partial"]) == ("2026-01-19 00:00:00", "2026-01-21 23:59:59", True)
    meta = run_dir.read_json(folder, run_dir.META)
    assert (meta["start_str"], meta["end_str"]) == ("2026-01-05 00:00:00", "2026-01-21 23:59:59")
    assert all(t.close_ts <= w04_end("2026-01-21") for t in run_dir.read_trades(folder))

    write_days(data_root)   # the rest of the week arrives
    second = run(env, weeks_range(W04, W04))   # no --force
    w04 = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, W04.key), run_dir.META)
    assert (w04["end_str"], w04["partial"], w04["batch_id"]) == ("2026-01-25 23:59:59", False, second.batch_id)
    w03 = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, W03.key), run_dir.META)
    assert w03["batch_id"] == first.batch_id, "a finished week is not run again"
    assert run_dir.read_json(folder, run_dir.META)["end_str"] == "2026-01-25 23:59:59"

    # And it is then the same week a run with all the data from the start computes.
    whole = make_env("whole")
    run(whole, weeks_range(W04, W04))
    a = run_dir.week_dir(env.work_dir, folder.name, W04.key)
    b = run_dir.week_dir(whole.work_dir, the_run(whole).name, W04.key)
    assert (a / run_dir.TRADES).read_bytes() == (b / run_dir.TRADES).read_bytes()


def w04_end(day: str) -> float:
    return dt.datetime.combine(dt.date.fromisoformat(day) + dt.timedelta(days=1), dt.time(), JST).timestamp()


def test_a_first_week_starts_where_the_data_does(make_env, data_root):
    env = make_env("env")
    remove_days(data_root, FIRST_DAY, dt.date(2026, 1, 6))   # the data starts on Wednesday 01-07
    run(env, weeks_range(W02, W03))
    folder = the_run(env)
    w02 = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, W02.key), run_dir.META)
    assert (w02["start_str"], w02["partial"]) == ("2026-01-07 00:00:00", True)
    assert run_dir.read_json(folder, run_dir.META)["start_str"] == "2026-01-07 00:00:00"


def test_no_data_at_all_runs_nothing(make_env, data_root, caplog):
    env = make_env("env")
    remove_days(data_root, FIRST_DAY, LAST_DAY)
    with caplog.at_level("WARNING"):
        run(env, weeks_range(W02, W03))
    assert not (Path(env.work_dir) / run_dir.RUN_LOG_DIR).exists()
    assert any("nothing to run" in r.message for r in caplog.records)
