"""Walk-forward over the selection rule's own parameters: window, history, reselection interval."""

import datetime as dt
import json

import pytest
from qate.util.dt_range import DtRange

from enoshima import run_dir
from enoshima.ledger import Trade, aggregate
from enoshima.post_analysis import walk_forward as wf
from enoshima.selection import SelectionSettings

PNL5 = SelectionSettings(score="pnl5", neighbors=False)


def trade(day: str, pnl: float, hour: int = 12) -> Trade:
    ts = DtRange.from_strings(day, day).start_ts + hour * 3600
    return Trade(ts, ts - 60, "X", "Y", "long", 1.0, 1.0, pnl, 0.0, pnl, pnl, pnl)


def run(run_id: str, params: dict, changes: dict[str, list[float]]) -> dict:
    """A loaded run, as `best.load_runs(root, "day")` gives it, from each day's PnL changes."""
    trades = [trade(day, c, hour=1 + i) for day, cs in sorted(changes.items()) for i, c in enumerate(cs)]
    return {"run_id": run_id, "params": params,
            "result": {"periods": aggregate(trades, "day"),
                       "trade_pnl5": {day: list(cs) for day, cs in changes.items()}}}


def span(start: str, n: int) -> list[str]:
    return wf.days(dt.date.fromisoformat(start), dt.date.fromisoformat(start) + dt.timedelta(days=n))


def test_spans_and_calendar_steps():
    assert wf.parse_span("6m") == (6, "m") and wf.parse_span("2w") == (2, "w")
    for bad in ("0d", "m", "1y", "1.5m"):
        with pytest.raises(ValueError):
            wf.parse_span(bad)
    assert wf.advance(dt.date(2026, 1, 31), (1, "m")) == dt.date(2026, 2, 28)   # the month's last day
    assert wf.advance(dt.date(2026, 3, 31), (1, "m"), back=True) == dt.date(2026, 2, 28)
    assert wf.advance(dt.date(2025, 12, 15), (2, "m")) == dt.date(2026, 2, 15)
    assert wf.advance(dt.date(2026, 1, 1), (2, "w")) == dt.date(2026, 1, 15)
    with pytest.raises(ValueError):
        wf.Rule(every="sometimes")


def test_only_complete_jst_days_count():
    r = DtRange.from_strings("2026-01-01", "2026-01-31")
    assert wf.complete_days(r.start_ts, r.end_ts) == (dt.date(2026, 1, 1), dt.date(2026, 2, 1))
    partial = DtRange.from_strings("20260101_0900", "20260131_1200")
    assert wf.complete_days(partial.start_ts, partial.end_ts) == (dt.date(2026, 1, 2), dt.date(2026, 1, 31))


def test_an_expanding_rule_takes_one_history_and_combinations_are_unique():
    got = wf.rules(["expanding", "rolling"], ["1m", "3m"], ["1w", "never"])
    assert [r.label for r in got] == ["expanding, every 1w", "expanding, once",
                                      "rolling 1m, every 1w", "rolling 1m, once",
                                      "rolling 3m, every 1w", "rolling 3m, once"]
    assert all(r.history == "3m" for r in got if r.window == "expanding")
    assert wf.default_test_from(dt.date(2026, 1, 1), got) == dt.date(2026, 4, 1)


def test_each_fold_chooses_on_the_days_before_it_and_trades_until_the_next_choice():
    # "early" wins the first week and loses after; "late" the opposite.
    first, stop = dt.date(2026, 1, 1), dt.date(2026, 1, 22)
    early = run("e", {"x": 1}, {**{d: [10.0] for d in span("2026-01-01", 7)}, **{d: [-1.0] for d in span("2026-01-08", 14)}})
    late = run("l", {"x": 2}, {**{d: [1.0] for d in span("2026-01-01", 7)}, **{d: [5.0] for d in span("2026-01-08", 14)}})
    test_from = dt.date(2026, 1, 8)

    weekly = wf.walk([early, late], [], PNL5, first, stop, test_from, wf.Rule("rolling", "1w", "1w"))
    assert [(f["train"], f["test"], f["run_id"]) for f in weekly["folds"]] == [
        (["2026-01-01", "2026-01-07"], ["2026-01-08", "2026-01-14"], "e"),   # early led its week
        (["2026-01-08", "2026-01-14"], ["2026-01-15", "2026-01-21"], "l"),   # late led the next
    ]
    assert weekly["out_of_sample"]["pnl5"] == pytest.approx(-7.0 + 35.0)
    assert weekly["out_of_sample"]["switches"] == 1

    # Expanding over both weeks, early still leads on 2026-01-15: 70 - 7 against 7 + 35.
    expanding = wf.walk([early, late], [], PNL5, first, stop, test_from, wf.Rule("expanding", "1w", "1w"))
    assert [f["run_id"] for f in expanding["folds"]] == ["e", "e"]
    assert [f["train"][0] for f in expanding["folds"]] == ["2026-01-01", "2026-01-01"]

    once = wf.walk([early, late], [], PNL5, first, stop, test_from, wf.Rule("rolling", "1w", "never"))
    assert [f["test"] for f in once["folds"]] == [["2026-01-08", "2026-01-21"]]
    assert once["out_of_sample"]["pnl5"] == pytest.approx(-14.0)


def test_the_out_of_sample_drawdown_is_exact_across_a_change_of_set():
    first, stop, test_from = dt.date(2026, 1, 1), dt.date(2026, 1, 4), dt.date(2026, 1, 2)
    a = run("a", {"x": 1}, {"2026-01-01": [5.0], "2026-01-02": [4.0, -6.0], "2026-01-03": [9.0]})
    b = run("b", {"x": 2}, {"2026-01-01": [1.0], "2026-01-02": [6.0], "2026-01-03": [3.0, -8.0]})
    # Daily: 01-02 on a (5 against 1), 01-03 on b (7 against 3).
    got = wf.walk([a, b], [], PNL5, first, stop, test_from, wf.Rule("expanding", "1d", "1d"))
    assert [f["run_id"] for f in got["folds"]] == ["a", "b"]
    # The curve: +4, -6 (from a), then +3, -8 (from b): high 4, low -7.
    assert got["out_of_sample"]["pnl5"] == pytest.approx(-7.0)
    assert got["out_of_sample"]["dd5"] == pytest.approx(11.0)


def test_a_fold_where_nothing_passes_sits_out():
    first, stop, test_from = dt.date(2026, 1, 1), dt.date(2026, 1, 3), dt.date(2026, 1, 2)
    losing = run("x", {"x": 1}, {"2026-01-01": [-1.0], "2026-01-02": [5.0]})
    got = wf.walk([losing], [], SelectionSettings(neighbors=False), first, stop, test_from, wf.Rule("expanding", "1d", "1d"))
    assert got["folds"][0]["params"] is None and got["out_of_sample"]["pnl5"] == 0.0
    assert got["out_of_sample"]["sat_out"] == 1


def write_run(root, run_id, params, changes, start="2026-01-01", end="2026-01-21"):
    r = DtRange.from_strings(start, end)
    trades = [trade(day, c, hour=1 + i) for day, cs in sorted(changes.items()) for i, c in enumerate(cs)]
    meta = {"params": params, "strategy": "s.v1", "start_str": r.start_str, "end_str": r.end_str}
    total = sum(c for cs in changes.values() for c in cs)
    run_dir.write(root, run_id, meta, {"pnl0": total, "pnl5": total, "pnl10": total, "pnl_cnt5": len(trades),
                                       "max_drawdown5": 0.0}, trades)


def test_the_command_writes_every_group_and_rule_and_the_sums(tmp_path):
    for mode, box, pnl in [("a", 1, 1.0), ("a", 2, 2.0), ("b", 1, 3.0), ("b", 2, -1.0)]:
        write_run(tmp_path, f"{mode}{box}", {"mode": mode, "box": box}, {d: [pnl] for d in span("2026-01-01", 21)})

    rule_list = wf.rules(["expanding"], ["1w"], ["1w", "never"])
    path = wf.run(tmp_path, rule_list, SelectionSettings(neighbors=False), sum_names=[["a", "b"]])
    report = json.loads(path.read_text())
    assert path.parent.name == "post_analysis" and path.with_suffix(".txt").is_file()

    groups = {g["group"]["mode"]: g for g in report["groups"]}
    assert set(groups) == {"a", "b"} and groups["a"]["test"] == ["2026-01-08", "2026-01-21"]
    assert [r["label"] for r in groups["a"]["rules"]] == ["expanding, every 1w", "expanding, once"]
    assert groups["a"]["hindsight"]["run_id"] == "a2" and groups["b"]["hindsight"]["run_id"] == "b1"
    assert groups["a"]["rules"][0]["out_of_sample"]["pnl5"] == pytest.approx(28.0)   # 14 days of a2

    [summed] = report["sums"]
    assert summed["name"] == "a + b"
    assert [r["pnl5"] for r in summed["rows"]] == pytest.approx([28.0 + 42.0] * 3)
