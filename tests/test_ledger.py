"""Trades, their groupings, and the run folder they are written to."""

import pytest
from qate.util.dt_range import DtRange

from enoshima import run_dir
from enoshima.ledger import GROUPINGS, Trade, aggregate, holding_bucket, streaks

# 2026-01-05 is a Monday; DtRange strings are JST.
MONDAY_0900 = DtRange.from_strings("20260105_0900", "20260105_0900").start_ts


def trade(ts, change, held=120.0, side="long"):
    return Trade(ts, ts - held, "EX", "SYM", side, 100.0, 100.0, change, 0.1, change, change - 1, change - 2)


def test_a_group_tracks_its_own_peak_trough_and_drawdown():
    trades = [trade(MONDAY_0900 + i, d) for i, d in enumerate([5.0, -8.0, 4.0, -2.0])]
    row = aggregate(trades, "day")["2026-01-05"]
    assert row["trades"] == 4 and row["wins0"] == 2
    assert row["pnl0"] == pytest.approx(-1.0)
    assert row["peak0"] == 5.0 and row["trough0"] == -3.0 and row["dd0"] == 8.0


def test_fixed_groupings_list_every_key_in_order_and_calendar_ones_only_what_traded():
    trades = [trade(MONDAY_0900, 1.0), trade(MONDAY_0900 + 86400 * 2 + 3600 * 5, 1.0)]  # Mon 09, Wed 14
    weekday = aggregate(trades, "weekday")
    assert list(weekday) == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert [weekday[d]["trades"] for d in ("Mon", "Tue", "Wed")] == [1, 0, 1]
    assert len(aggregate(trades, "hour")) == 24 and len(aggregate(trades, "monthday")) == 31
    assert len(aggregate(trades, "weekday_hour")) == 7 * 24
    assert aggregate(trades, "weekday_hour")["Wed 14"]["trades"] == 1
    assert list(aggregate(trades, "day")) == ["2026-01-05", "2026-01-07"]
    assert list(aggregate(trades, "week")) == ["2026-W02"]


def test_holding_time_and_side():
    assert [holding_bucket(s) for s in (59, 60, 299, 1800, 7199, 28800, None)] == [
        "<1m", "1-5m", "1-5m", "30m-2h", "30m-2h", ">8h", "unknown"]
    trades = [trade(MONDAY_0900, 1.0, held=10), trade(MONDAY_0900 + 1, -1.0, held=4000, side="short")]
    holding = aggregate(trades, "holding")
    assert "unknown" not in holding, "listed only when a trade has no open time"
    assert holding["<1m"]["pnl0"] == 1.0 and holding["30m-2h"]["pnl0"] == -1.0
    assert aggregate(trades, "side")["short"]["trades"] == 1


def test_streaks():
    trades = [trade(MONDAY_0900 + i, d) for i, d in enumerate([1.0, 1.0, -1.0, -1.0, -1.0, 1.0])]
    got = streaks(trades)
    assert got["max_win_streak0"] == 2 and got["max_loss_streak0"] == 3
    assert got["max_win_streak10"] == 0 and got["max_loss_streak10"] == 6   # change - 2: all losses


def test_a_folder_is_complete_only_once_written_and_force_replaces_it(tmp_path):
    trades = [trade(MONDAY_0900, 1.0)]
    # An interrupted write leaves a temporary folder that nothing counts.
    (tmp_path / "run_log" / "abc.tmp-1").mkdir(parents=True)
    (tmp_path / "run_log" / "abc.tmp-1" / run_dir.SUMMARY).write_text("{}")
    assert list(run_dir.iter_complete(tmp_path)) == []

    path = run_dir.write(tmp_path, "abc", {"id": "abc"}, {"pnl0": 1.0}, trades)
    assert list(run_dir.iter_complete(tmp_path)) == [path]
    assert {f"by_{name}.json" for name in GROUPINGS} <= {p.name for p in path.iterdir()}

    run_dir.write(tmp_path, "abc", {"id": "abc"}, {"pnl0": 2.0}, [])
    assert run_dir.read_json(path, run_dir.SUMMARY) == {"pnl0": 2.0}
    assert (path / run_dir.TRADES).read_text().count("\n") == 1   # the header only
    assert sorted(p.name for p in (tmp_path / "run_log").iterdir()) == ["abc", "abc.tmp-1"]
