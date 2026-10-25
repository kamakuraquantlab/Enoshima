"""Strict weekly runs: weeks computed once, and any window of them the same as one run of it.

The data is a sine wave of books, one a minute, over three ISO weeks, the
day before them and the day after, so the example strategy trades through every week and across
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
from test_run_end_to_end import ENV_NAME, EXAMPLE_ROOT, FEES, MARKET, MemoryStore

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
LAST_DAY = dt.date(2026, 1, 26)  # the Monday after 2026-W04: the overlap
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
    runner = Runner(env, dt_range, fee_rates=FEES, chunk_days=2, weekly=WeeklySettings(), **kwargs)
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
    # The preload day 10-04, the week, and the overlap day 10-12.
    assert week.replay_range(WeeklySettings()).start_str == "2026-10-04 00:00:00"
    assert week.replay_range(WeeklySettings()).days == [f"2026-10-{d:02d}" for d in range(4, 13)]
    assert week.replay_range(WeeklySettings(preload_days=2, overlap_days=0)).days == [
        f"2026-10-{d:02d}" for d in range(3, 12)
    ]


def test_the_covering_weeks_are_the_fewest_that_contain_the_range():
    weeks = weekly.covering(DtRange.from_strings("2026-12-31", "2027-01-11"))
    assert [w.key for w in weeks] == ["2026-W53", "2027-W01", "2027-W02"]
    assert [Week.parse(w.key) for w in weeks] == weeks
    # A Sunday is the end of its week, a Monday the start of the next.
    assert [w.key for w in weekly.covering(DtRange.from_strings("2026-10-11", "2026-10-12"))] == [
        "2026-W41",
        "2026-W42",
    ]


def test_the_preload_and_overlap_are_checked():
    for name in ("preload_days", "overlap_days"):
        for bad in (-1, 1.5, True, "1"):
            with pytest.raises(ValueError, match=name):
                WeeklySettings(**{name: bad})


# ---------------------------------------------------------------- the runs


def test_a_range_is_widened_to_its_weeks(make_env):
    env = make_env("env")
    runner = run(env, DtRange.from_strings("2026-01-07", "2026-01-08"))
    assert runner.weeks == [W02]
    assert (runner.dt_range.start_str, runner.dt_range.end_str) == ("2026-01-05 00:00:00", "2026-01-11 23:59:59")

    folder = the_run(env)
    assert sorted(p.name for p in (folder / run_dir.WEEKS_DIR).iterdir()) == ["2026-W02"]
    meta = run_dir.read_json(folder, run_dir.META)
    assert (meta["mode"], meta["preload_days"], meta["overlap_days"], meta["weeks"]) == ("weekly", 1, 1, 1)
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
    runner = Runner(env, weeks_range(W02, W03), optimize="grid", fee_rates=FEES, chunk_days=2, weekly=WeeklySettings())
    runner.run()
    assert not runner.failed
    [group] = best.recommend(Path(env.work_dir), runner.batch_id, SelectionSettings())
    assert group["runs"] == 6
    assert (group["start_str"], group["end_str"]) == ("2026-01-05 00:00:00", "2026-01-18 23:59:59")


def test_the_run_id_has_no_dates_but_has_the_preload_and_overlap(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    first = the_run(env).name
    run(env, weeks_range(W03, W03))
    assert the_run(env).name == first, "a later week goes in the same folder"

    Runner(env, weeks_range(W02, W02), fee_rates=FEES, chunk_days=2, weekly=WeeklySettings(preload_days=0)).run()
    Runner(env, weeks_range(W02, W02), fee_rates=FEES, chunk_days=2, weekly=WeeklySettings(overlap_days=0)).run()
    assert len(list((Path(env.work_dir) / run_dir.RUN_LOG_DIR).iterdir())) == 3


def test_a_new_strategy_revision_is_a_new_folder(make_env, monkeypatch):
    """Weeks of two versions of the strategy are never put together."""
    import enoshima.runner

    env = make_env("env")
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: "aaaaaaaaaaaa")
    run(env, weeks_range(W02, W02))
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: "bbbbbbbbbbbb")
    run(env, weeks_range(W03, W03))

    folders = sorted((Path(env.work_dir) / run_dir.RUN_LOG_DIR).iterdir())
    assert len(folders) == 2
    revs = {run_dir.read_json(f, run_dir.META)["strategy_rev"]: [w.key for w in weekly.on_disk(env.work_dir, f.name)]
            for f in folders}
    assert revs == {"aaaaaaaaaaaa": ["2026-W02"], "bbbbbbbbbbbb": ["2026-W03"]}


# What differs between two runs that computed the same numbers: when, and in which batch.
RUN_FIELDS = {"batch_id", "start_ts", "end_ts", "execution_time"}


def test_an_extended_window_replays_only_its_new_week_and_equals_one_run(make_env):
    """The requirement: weeks 2-3, then week 4 alone, gives byte for byte what 2-4 at once gives.

    Every file of the folder, the root's included -- what `select` and
    `walk-forward` read -- but for when each week ran and in which batch.
    """
    stepwise = make_env("stepwise")
    first = run(stepwise, weeks_range(W02, W03))
    second = run(stepwise, weeks_range(W04, W04))

    at_once = make_env("at_once")
    run(at_once, weeks_range(W02, W04))

    a, b = the_run(stepwise), the_run(at_once)
    assert a.name == b.name
    files = sorted(str(f.relative_to(a)) for f in a.rglob("*") if f.is_file())
    assert files == sorted(str(f.relative_to(b)) for f in b.rglob("*") if f.is_file())
    assert {"trades.csv", "summary.json", "by_week.json", "weeks/2026-W04/trades.csv"} <= set(files)
    for name in files:
        if Path(name).name == run_dir.META:
            meta_a, meta_b = (run_dir.read_json((x / name).parent, run_dir.META) for x in (a, b))
            assert {k: v for k, v in meta_a.items() if k not in RUN_FIELDS} == \
                   {k: v for k, v in meta_b.items() if k not in RUN_FIELDS}, name
        else:
            assert (a / name).read_bytes() == (b / name).read_bytes(), name

    # The second run computed W04 only; the first two weeks are the first run's.
    for week in (W02, W03, W04):
        batch = run_dir.read_json(run_dir.week_dir(stepwise.work_dir, a.name, week.key), run_dir.META)["batch_id"]
        assert batch == (second.batch_id if week == W04 else first.batch_id)

    trades, summary = weekly.window(stepwise.work_dir, a.name, [W02, W03, W04])
    assert summary["pnl_cnt0"] >= 9, "the strategy must trade in every week for this to test anything"
    weeks = [run_dir.read_json(run_dir.week_dir(stepwise.work_dir, a.name, w.key), run_dir.SUMMARY)
             for w in (W02, W03, W04)]
    assert all(w["pnl_cnt0"] > 0 for w in weeks)
    assert summary["pnl_cnt0"] == sum(w["pnl_cnt0"] for w in weeks) == len(trades)
    assert summary["pnl5"] == pytest.approx(sum(w["pnl5"] for w in weeks))
    assert all("open_at_end" in w for w in weeks)


def test_a_trade_is_the_week_it_opened_in(make_env):
    """Wherever it closed: one opened on a Sunday and closed on the Monday is the Sunday's week's."""
    env = make_env("env")
    run(env, weeks_range(W02, W04))
    folder = the_run(env)
    for week in (W02, W03, W04):
        trades = run_dir.read_trades(run_dir.week_dir(env.work_dir, folder.name, week.key))
        assert all(week.dt_range.start_ts <= t.open_ts <= week.dt_range.end_ts for t in trades), week.key
    w02 = run_dir.read_trades(run_dir.week_dir(env.work_dir, folder.name, W02.key))
    assert any(t.close_ts >= W03.start.timestamp() for t in w02), "no trade closes in W02's overlap"
    w04 = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, W04.key), run_dir.SUMMARY)
    assert w04["open_at_end"] == 0, "the overlap closes everything the week opened"


def test_a_window_equals_one_continuous_replay_when_the_preload_holds_the_state(make_env):
    """Not guaranteed in general -- see `weekly` -- but true of a strategy whose state fits in the preload.

    The example's slowest average is 120 books, two hours here, so the 1-day
    preload re-creates a position open across a Monday, and the 1-day overlap
    sees every position closed. If this breaks, a week started cold has
    stopped matching the same week inside a longer run.
    """
    env = make_env("weekly")
    run(env, weeks_range(W02, W04))
    stitched, summary = weekly.window(env.work_dir, the_run(env).name, [W02, W03, W04])

    # One replay over the same days, its trades taken as the weeks take theirs: by open.
    continuous_env = make_env("continuous")
    Runner(continuous_env, DtRange(W02.start, W04.dt_range.end_dt + dt.timedelta(days=1)), fee_rates=FEES, chunk_days=2).run()
    [path] = run_dir.iter_complete(Path(continuous_env.work_dir))
    span = weeks_range(W02, W04)
    continuous = [t for t in run_dir.read_trades(path) if span.start_ts <= t.open_ts <= span.end_ts]

    assert any(t.open_ts < W03.start.timestamp() <= t.close_ts for t in stitched), "no trade crosses a Monday"
    assert [(t.close_ts, t.open_ts) for t in stitched] == [(t.close_ts, t.open_ts) for t in continuous]
    for a, b in zip(stitched, continuous):
        assert a.pnl_change5 == pytest.approx(b.pnl_change5)
    assert summary["pnl5"] == pytest.approx(sum(t.pnl_change5 for t in continuous))


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
    Runner(env, DtRange.from_strings("2026-01-05", "2026-01-09"), fee_rates=FEES, chunk_days=2).run()
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


def test_a_week_writes_nothing_of_its_preload_or_overlap_but_its_own_closes(make_env):
    """The overlap day is the next week's: written by both, it would be in the bucket twice."""
    env = make_env("env")
    store = DeletingStore()
    run(env, weeks_range(W02, W02), influx=store)
    [writer] = store.writers
    week = W02.dt_range
    outside = [p for p in writer.points if not week.start_ts * 1000 <= p._time <= week.end_ts * 1000]
    assert outside, "no trade closes in the overlap, so this tests nothing"
    for point in outside:
        assert point._time > week.end_ts * 1000
        assert point._name in ("EVAL", "ORDER") or (point._name == "PNL" and point._tags["time_align"] == "close")


def test_a_week_stops_as_soon_as_its_last_position_is_closed(make_env, monkeypatch):
    """The overlap is only there to see the week's trades closed; nothing after the last of them is replayed."""
    from enoshima.result import ResultReporter

    stopped = []
    original = ResultReporter.settled

    def settled(self, ts, pending_opens):
        done = original(self, ts, pending_opens)
        if done:
            stopped.append(ts)
        return done

    monkeypatch.setattr(ResultReporter, "settled", settled)
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    trades = run_dir.read_trades(run_dir.week_dir(env.work_dir, the_run(env).name, W02.key))

    [ts] = stopped
    assert max(t.close_ts for t in trades) <= ts < W03.start.timestamp() + 86400
    assert ts > W02.dt_range.end_ts


def test_no_open_fills_in_the_overlap():
    """An add-on filled there would move a week's position's `open_ts` into the next week.

    The example strategy holds one position at a time and the replay stops once
    it is flat, so it never opens in the overlap: the gateway is tested alone.
    """
    from qate.core.model import ExchangeName, Market, OrderBook, OrderLevel, SettleType, Side
    from qate.core.order import OrderRequest, OrderType
    from qate.core.symbol import Symbol

    from enoshima.backtest import OverlapGateway

    market = Market(ExchangeName.GMO, Symbol.BTC_JPY)
    end = W02.dt_range.end_ts
    seen = []
    gateway = OverlapGateway(ExchangeName.GMO, opens_until=end)
    gateway.add_order_listener(type("L", (), {"put": lambda self, event: seen.append(event)})())

    def order(ctx_id, ts, settle_type):
        request = OrderRequest(ts=ts, market=market, side=Side.BUY, price=BASE_PRICE, size=0.01,
                               settle_type=settle_type, order_type=OrderType.DEFAULT)
        request.ctx_id = ctx_id
        return request

    gateway.create(order(1, end - 1, SettleType.OPEN))      # the week's: fills
    gateway.create(order(2, end + 60, SettleType.OPEN))     # the overlap's: taken, never filled
    gateway.create(order(3, end + 60, SettleType.CLOSE))    # a close: fills
    gateway.handle_order_book(OrderBook([OrderLevel(BASE_PRICE - 500, 1.0)], [OrderLevel(BASE_PRICE + 500, 1.0)],
                                        market.symbol, market.exchange_name, end + 120))
    created = [e[1].order_request.ctx_id for e in seen if e[0] == "ORDER_CREATED"]
    filled = [e[1].order_request.ctx_id for e in seen if e[0] == "ORDER_FILLED"]
    assert (created, filled) == ([1, 2, 3], [1, 3])
    gateway.cancel(order(2, end + 180, SettleType.OPEN))    # nothing to cancel, and no error
    assert not any(e[0] in ("ORDER_CANCELLED", "ORDER_ERROR") for e in seen)


def test_a_weeks_orders_are_its_trades_legs(make_env):
    """No order of the preload's trades, nor of the overlap's: one to open and one to close each trade."""
    env = make_env("env")
    run(env, weeks_range(W02, W04))
    folder = the_run(env)
    for week in (W02, W03, W04):
        summary = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, week.key), run_dir.SUMMARY)
        assert summary["order_cnt0"] == 2 * summary["pnl_cnt0"], week.key


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

    # Wednesday is Tuesday's overlap: W04 has the trades opened Monday and Tuesday.
    w04 = run_dir.read_json(run_dir.week_dir(env.work_dir, folder.name, W04.key), run_dir.META)
    assert (w04["start_str"], w04["end_str"], w04["partial"]) == ("2026-01-19 00:00:00", "2026-01-20 23:59:59", True)
    meta = run_dir.read_json(folder, run_dir.META)
    assert (meta["start_str"], meta["end_str"]) == ("2026-01-05 00:00:00", "2026-01-20 23:59:59")
    assert all(t.open_ts <= w04_end("2026-01-20") for t in run_dir.read_trades(folder))
    assert all(t.close_ts <= w04_end("2026-01-21") for t in run_dir.read_trades(folder))

    write_days(data_root)   # the rest of the week, and its overlap, arrive
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
