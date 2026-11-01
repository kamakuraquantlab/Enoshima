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
from komachi import settings as komachi_settings
from qate.util.dt_range import DtRange
from qate_env.env import Env
from test_run_end_to_end import ENV_NAME, EXAMPLE_ROOT, FEES, MARKET, RunRef

from enoshima import lake, weekly
from enoshima.eval import SLIPPAGE_05, SLIPPAGE_10
from enoshima.lake import Store
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


# Each env's own data lake, by env root: two envs of one name compared here
# must not write into one lake. Their bronze is one tree, linked into each.
LAKES: dict[str, Path] = {}


def use(env: Env) -> Store:
    """Point komachi -- and so every run -- at `env`'s lake, and return its store."""
    root = LAKES[env.root_dir]
    # conftest's temporary settings file; never the user's, which a `monkeypatch.undo()` would restore.
    assert Path(komachi_settings.ENV_FILE).is_relative_to(root.parent.parent), komachi_settings.ENV_FILE
    komachi_settings.ENV_FILE.write_text(f"ROOT_PATH={root}\n")
    return Store(root, str(ENV_NAME))


@pytest.fixture
def make_env(tmp_path, data_root, monkeypatch):
    """A function that copies the example environment to a fresh env root and lake; the data is shared."""
    write_days(data_root)
    monkeypatch.chdir(tmp_path)

    def make(name: str) -> Env:
        env_root = tmp_path / name
        shutil.copytree(EXAMPLE_ROOT / "env", env_root)
        env = Env(str(env_root), ENV_NAME)
        lake_root = tmp_path / "lakes" / name
        lake_root.mkdir(parents=True)
        (lake_root / "bronze").symlink_to(data_root / "bronze")
        LAKES[env.root_dir] = lake_root
        use(env)
        return env

    return make


def weeks_range(first: Week, last: Week) -> DtRange:
    return DtRange(first.start, last.dt_range.end_dt)


def run(env: Env, dt_range: DtRange, **kwargs) -> Runner:
    use(env)
    runner = Runner(env, dt_range, fee_rates=FEES, chunk_days=2, weekly=WeeklySettings(), **kwargs)
    runner.run()
    assert not runner.failed
    return runner


def the_run(env: Env) -> RunRef:
    store = use(env)
    [run_id] = store.all_run_ids()
    return RunRef(store, run_id)


def meta(ref: RunRef, week: Week | None = None) -> dict:
    """The run's meta, or one week's."""
    return ref.store.read_run(ref.name)["meta"] if week is None else ref.store.read_part(ref.name, week.key)["meta"]


def summary(ref: RunRef, week: Week | None = None) -> dict:
    return ref.store.read_run(ref.name)["summary"] if week is None else ref.store.read_part(ref.name, week.key)["summary"]


def week_trades(ref: RunRef, week: Week):
    return ref.store.read_part_trades(ref.name, week.key)


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
    assert folder.store.parts(folder.name) == ["2026-W02"]
    root = meta(folder)
    assert (root["mode"], root["preload_days"], root["overlap_days"], root["weeks"]) == ("weekly", 1, 1, 1)
    assert "batch_id" not in root and "week" not in root, "the run's row holds what every week shares"
    week_meta = meta(folder, W02)
    assert (week_meta["week"], week_meta["start_str"]) == ("2026-W02", "2026-01-05 00:00:00")
    assert week_meta["batch_id"] == runner.batch_id


def test_the_root_is_rebuilt_from_every_week_on_disk(make_env):
    """Not the current run's result: the weeks', over the first Monday to the last Sunday."""
    env = make_env("env")
    run(env, weeks_range(W02, W03))
    run(env, weeks_range(W04, W04))
    folder = the_run(env)

    root = meta(folder)
    assert (root["start_str"], root["end_str"], root["weeks"]) == ("2026-01-05 00:00:00", "2026-01-25 23:59:59", 3)
    trades, window = weekly.window(folder.store, folder.name, [W02, W03, W04])
    assert summary(folder) == json_round_trip(window)
    assert folder.store.trades(folder.name) == trades
    assert sum(r["trades"] for r in folder.store.grouping(folder.name, "week").values()) == len(trades)
    # An ordinary run to everything that reads them.
    assert folder.store.run_ids() == [folder.name]

    # Asking again for an earlier week alone runs nothing and keeps the whole range.
    run(env, weeks_range(W02, W02))
    assert meta(folder)["end_str"] == "2026-01-25 23:59:59"


def json_round_trip(data):
    import json

    from qate.util.encoder import Encoder

    return json.loads(json.dumps(data, cls=Encoder))


def test_a_gap_between_the_weeks_on_disk_and_those_asked_for_is_filled(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    runner = run(env, weeks_range(W04, W04))
    folder = the_run(env)
    assert [w.key for w in weekly.on_disk(folder.store, folder.name)] == ["2026-W02", "2026-W03", "2026-W04"]
    assert meta(folder, W03)["batch_id"] == runner.batch_id
    assert meta(folder)["weeks"] == 3


def test_a_root_whose_weeks_have_a_gap_is_not_complete_until_filled(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W04))
    folder = the_run(env)
    folder.store.path(lake.PART, folder.name, W03.key).unlink()   # as if W03 had failed

    assert not weekly.rebuild(folder.store, folder.name, meta(folder))
    assert not folder.store.is_complete(folder.name), "a summary over W02..W04 would claim a week it does not have"
    assert folder.store.run_ids() == []

    run(env, weeks_range(W04, W04))
    assert folder.store.is_complete(folder.name)
    assert meta(folder)["weeks"] == 3


def test_a_week_whose_files_went_missing_is_refused_not_read_as_empty(make_env):
    """A file removed from under a finished week would otherwise read as a week that traded nothing."""
    env = make_env("env")
    run(env, weeks_range(W02, W03))
    folder = the_run(env)
    folder.store.path(lake.TRADE, folder.name, W03.key).unlink()
    with pytest.raises(lake.IncompletePart, match="2026-W03"):
        weekly.window(folder.store, folder.name, [W02, W03])


def test_a_weekly_sweep_s_rows_cover_its_weeks_like_any_run(make_env):
    env = make_env("env")
    runner = Runner(env, weeks_range(W02, W03), optimize="grid", fee_rates=FEES, chunk_days=2, weekly=WeeklySettings())
    runner.run()
    assert not runner.failed
    store = use(env)
    rows = [store.read_run(run_id)["meta"] for run_id in store.run_ids()]
    assert len(rows) == 6 and not any(m["is_temporary_run"] for m in rows)
    assert {(m["start_str"], m["end_str"]) for m in rows} == {("2026-01-05 00:00:00", "2026-01-18 23:59:59")}


def test_the_run_id_has_no_dates_but_has_the_preload_and_overlap(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    first = the_run(env).name
    run(env, weeks_range(W03, W03))
    assert the_run(env).name == first, "a later week goes in the same run"

    Runner(env, weeks_range(W02, W02), fee_rates=FEES, chunk_days=2, weekly=WeeklySettings(preload_days=0)).run()
    Runner(env, weeks_range(W02, W02), fee_rates=FEES, chunk_days=2, weekly=WeeklySettings(overlap_days=0)).run()
    assert len(use(env).all_run_ids()) == 3


def test_a_new_strategy_revision_is_a_new_run(make_env, monkeypatch):
    """Weeks of two versions of the strategy are never put together."""
    import enoshima.runner

    env = make_env("env")
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: "aaaaaaaaaaaa")
    run(env, weeks_range(W02, W02))
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: "bbbbbbbbbbbb")
    run(env, weeks_range(W03, W03))

    store = use(env)
    folders = [RunRef(store, r) for r in store.all_run_ids()]
    assert len(folders) == 2
    revs = {meta(f)["strategy_rev"]: [w.key for w in weekly.on_disk(store, f.name)] for f in folders}
    assert revs == {"aaaaaaaaaaaa": ["2026-W02"], "bbbbbbbbbbbb": ["2026-W03"]}


# What differs between two runs that computed the same numbers: when, and in which batch.
RUN_FIELDS = {"batch_id", "start_ts", "end_ts", "execution_time"}


def test_an_extended_window_replays_only_its_new_week_and_equals_one_run(make_env):
    """The requirement: weeks 2-3, then week 4 alone, gives byte for byte what 2-4 at once gives.

    Every file of the run in the lake, its row's included -- what `select` and
    `walk-forward` read -- but for when each week ran and in which batch.
    """
    stepwise = make_env("stepwise")
    first = run(stepwise, weeks_range(W02, W03))
    second = run(stepwise, weeks_range(W04, W04))

    at_once = make_env("at_once")
    run(at_once, weeks_range(W02, W04))

    a, b = the_run(stepwise), the_run(at_once)
    assert a.name == b.name

    def files(ref):
        return {dataset: sorted(str(f.relative_to(ref.store.run_dir(dataset, ref.name)))
                                for f in ref.store.run_dir(dataset, ref.name).rglob("*") if f.is_file())
                for dataset in lake.DATASETS}

    assert files(a) == files(b)
    assert files(a)[lake.TRADE] == [f"part={w.key}/data.parquet" for w in (W02, W03, W04)]
    assert files(a)[lake.ORDER] == files(a)[lake.TRADE]

    def same_row(x, y, name):
        assert {k: v for k, v in x["meta"].items() if k not in RUN_FIELDS} == \
               {k: v for k, v in y["meta"].items() if k not in RUN_FIELDS}, name
        assert x["summary"] == y["summary"], name

    same_row(a.store.read_run(a.name), b.store.read_run(b.name), "the run")
    for week in (W02, W03, W04):
        same_row(a.store.read_part(a.name, week.key), b.store.read_part(b.name, week.key), week.key)
        for dataset in lake.DATA:
            pa_, pb = a.store.path(dataset, a.name, week.key), b.store.path(dataset, b.name, week.key)
            if pa_.is_file():   # the example strategy emits no bars
                assert pa_.read_bytes() == pb.read_bytes(), (dataset, week.key)

    # The second run computed W04 only; the first two weeks are the first run's.
    for week in (W02, W03, W04):
        assert meta(a, week)["batch_id"] == (second.batch_id if week == W04 else first.batch_id)

    trades, window = weekly.window(a.store, a.name, [W02, W03, W04])
    assert window["pnl_cnt0"] >= 9, "the strategy must trade in every week for this to test anything"
    weeks = [summary(a, w) for w in (W02, W03, W04)]
    assert all(w["pnl_cnt0"] > 0 for w in weeks)
    assert window["pnl_cnt0"] == sum(w["pnl_cnt0"] for w in weeks) == len(trades)
    assert window["pnl5"] == pytest.approx(sum(w["pnl5"] for w in weeks))
    assert all("open_at_end" in w for w in weeks)


def test_a_trade_is_the_week_it_opened_in(make_env):
    """Wherever it closed: one opened on a Sunday and closed on the Monday is the Sunday's week's."""
    env = make_env("env")
    run(env, weeks_range(W02, W04))
    folder = the_run(env)
    for week in (W02, W03, W04):
        trades = week_trades(folder, week)
        assert all(week.dt_range.start_ts <= t.open_ts <= week.dt_range.end_ts for t in trades), week.key
    w02 = week_trades(folder, W02)
    assert any(t.close_ts >= W03.start.timestamp() for t in w02), "no trade closes in W02's overlap"
    assert summary(folder, W04)["open_at_end"] == 0, "the overlap closes everything the week opened"


def test_a_window_equals_one_continuous_replay_when_the_preload_holds_the_state(make_env):
    """Not guaranteed in general -- see `weekly` -- but true of a strategy whose state fits in the preload.

    The example's slowest average is 120 books, two hours here, so the 1-day
    preload re-creates a position open across a Monday, and the 1-day overlap
    sees every position closed. If this breaks, a week started cold has
    stopped matching the same week inside a longer run.
    """
    env = make_env("weekly")
    run(env, weeks_range(W02, W04))
    stitched, summary = weekly.window(use(env), the_run(env).name, [W02, W03, W04])

    # One replay over the same days, its trades taken as the weeks take theirs: by open.
    continuous_env = make_env("continuous")
    Runner(continuous_env, DtRange(W02.start, W04.dt_range.end_dt + dt.timedelta(days=1)), fee_rates=FEES, chunk_days=2).run()
    store = use(continuous_env)
    [run_id] = store.run_ids()
    span = weeks_range(W02, W04)
    continuous = [t for t in store.trades(run_id) if span.start_ts <= t.open_ts <= span.end_ts]

    assert any(t.open_ts < W03.start.timestamp() <= t.close_ts for t in stitched), "no trade crosses a Monday"
    assert [(t.close_ts, t.open_ts) for t in stitched] == [(t.close_ts, t.open_ts) for t in continuous]
    for a, b in zip(stitched, continuous):
        assert a.pnl_change5 == pytest.approx(b.pnl_change5)
    assert summary["pnl5"] == pytest.approx(sum(t.pnl_change5 for t in continuous))


def test_a_window_of_one_week_is_that_weeks_summary(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    stored = summary(the_run(env), W02)
    _, window = weekly.window(use(env), the_run(env).name, [W02])
    assert json_round_trip(window) == {k: v for k, v in stored.items() if not k.startswith("order_") and k != "open_at_end"}


def test_a_window_with_a_week_missing_is_refused(make_env):
    env = make_env("env")
    run(env, weeks_range(W02, W02))
    with pytest.raises(ValueError, match="2026-W03"):
        weekly.window(use(env), the_run(env).name, [W02, W03])


def test_a_trade_carries_both_legs_extra_slippage(make_env):
    """Charged at the close, so a position opened in the preload costs what any other does."""
    env = make_env("env")
    run(env, weeks_range(W02, W03))
    trades, _ = weekly.window(use(env), the_run(env).name, [W02, W03])
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
    assert meta(the_run(env), W02)["batch_id"] == first.batch_id

    forced = run(env, weeks_range(W02, W02), force=True)
    assert meta(the_run(env), W02)["batch_id"] == forced.batch_id


# ---------------------------------------------------------------- records


def test_a_weeks_records_are_its_own_and_a_rerun_replaces_them(make_env):
    """The overlap day is the next week's: recorded by both, its records would be in the lake twice."""
    env = make_env("env")
    run(env, weeks_range(W02, W03))
    folder = the_run(env)
    store = folder.store
    week = W02.dt_range
    pnl = store.records(lake.PNL, folder.name).to_pylist()
    w02 = [r for r in pnl if r["part"] == W02.key]
    # A week's PnL updates are its own positions': opened in the week, wherever they closed.
    for r in w02:
        opened = r["ts"] if r["SETTLE_TYPE"] == "OPEN" else r["open_ts"]
        assert week.start_ts <= opened <= week.end_ts
    assert any(r["ts"] > week.end_ts for r in w02), "no trade closes in the overlap, so this tests nothing"
    closes = sum(1 for r in w02 if r["SETTLE_TYPE"] == "CLOSE")
    assert closes == summary(folder, W02)["pnl_cnt0"]
    orders = store.records(lake.ORDER, folder.name).to_pylist()
    assert sum(1 for r in orders if r["part"] == W02.key) == summary(folder, W02)["order_cnt0"]

    rows = store.records(lake.PNL, folder.name).num_rows
    run(env, weeks_range(W03, W03), force=True)
    assert store.records(lake.PNL, folder.name).num_rows == rows


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
    trades = week_trades(the_run(env), W02)

    [ts] = stopped
    assert max(t.close_ts for t in trades) <= ts < W03.start.timestamp() + 86400
    assert ts > W02.dt_range.end_ts


def test_a_week_that_stops_early_leaves_no_loader_holding_its_data(make_env):
    """Every week stops before its data runs out; its chunk loader must not wait on, holding two chunks."""
    import threading

    env = make_env("env")
    run(env, weeks_range(W02, W04))
    assert [t for t in threading.enumerate() if t.name == "chunk-loader"] == []


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
        figures = summary(folder, week)
        assert figures["order_cnt0"] == 2 * figures["pnl_cnt0"], week.key


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
    assert [w.key for w in weekly.on_disk(folder.store, folder.name)] == ["2026-W02", "2026-W03", "2026-W04"]
    assert meta(folder)["end_str"] == "2026-01-25 23:59:59"


def test_a_last_week_is_run_as_far_as_the_data_goes_and_grows_when_it_arrives(make_env, data_root):
    env = make_env("env")
    remove_days(data_root, dt.date(2026, 1, 22), LAST_DAY)   # W04 has Monday to Wednesday
    first = run(env, weeks_range(W02, W04))
    folder = the_run(env)

    # Wednesday is Tuesday's overlap: W04 has the trades opened Monday and Tuesday.
    w04 = meta(folder, W04)
    assert (w04["start_str"], w04["end_str"], w04["partial"]) == ("2026-01-19 00:00:00", "2026-01-20 23:59:59", True)
    root = meta(folder)
    assert (root["start_str"], root["end_str"]) == ("2026-01-05 00:00:00", "2026-01-20 23:59:59")
    assert all(t.open_ts <= w04_end("2026-01-20") for t in folder.store.trades(folder.name))
    assert all(t.close_ts <= w04_end("2026-01-21") for t in folder.store.trades(folder.name))

    write_days(data_root)   # the rest of the week, and its overlap, arrive
    second = run(env, weeks_range(W04, W04))   # no --force
    w04 = meta(folder, W04)
    assert (w04["end_str"], w04["partial"], w04["batch_id"]) == ("2026-01-25 23:59:59", False, second.batch_id)
    assert meta(folder, W03)["batch_id"] == first.batch_id, "a finished week is not run again"
    assert meta(folder)["end_str"] == "2026-01-25 23:59:59"

    # And it is then the same week a run with all the data from the start computes.
    whole = make_env("whole")
    run(whole, weeks_range(W04, W04))
    a = folder.store.path(lake.TRADE, folder.name, W04.key)
    b = the_run(whole).store.path(lake.TRADE, the_run(whole).name, W04.key)
    assert a.read_bytes() == b.read_bytes()


def w04_end(day: str) -> float:
    return dt.datetime.combine(dt.date.fromisoformat(day) + dt.timedelta(days=1), dt.time(), JST).timestamp()


def test_a_first_week_starts_where_the_data_does(make_env, data_root):
    env = make_env("env")
    remove_days(data_root, FIRST_DAY, dt.date(2026, 1, 6))   # the data starts on Wednesday 01-07
    run(env, weeks_range(W02, W03))
    folder = the_run(env)
    w02 = meta(folder, W02)
    assert (w02["start_str"], w02["partial"]) == ("2026-01-07 00:00:00", True)
    assert meta(folder)["start_str"] == "2026-01-07 00:00:00"


def test_no_data_at_all_runs_nothing(make_env, data_root, caplog):
    env = make_env("env")
    remove_days(data_root, FIRST_DAY, LAST_DAY)
    with caplog.at_level("WARNING"):
        run(env, weeks_range(W02, W03))
    assert use(env).all_run_ids() == []
    assert any("nothing to run" in r.message for r in caplog.records)
