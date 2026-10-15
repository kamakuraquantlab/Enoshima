"""Choosing a parameter set: windows, filters, neighbours, and walk-forward."""

import random

import pytest
from qate.util.dt_range import DtRange

from enoshima.result import MonthLedger
from enoshima.selection import (
    SelectionSettings,
    complete_months,
    neighbours,
    rank,
    select,
    walk_forward,
    window_stats,
)

GRID = [
    {"name": "schedule", "values": ["a", "b"]},
    {"name": "box", "values": [0.0025, 0.003, 0.004]},
    {"name": "step", "values": [0.02, 0.04, 0.05]},
]


def periods_from(changes: dict[str, list[float]]) -> dict:
    """A run log's `periods` from each month's closed-PnL changes, the same at every slippage."""
    out = {}
    for month, deltas in changes.items():
        ledger = MonthLedger("5")
        for d in deltas:
            ledger.add(month, d)
        m = ledger.months[month]
        row = {"trades": m["trades"]}
        for s in ("0", "5", "10"):
            row.update({f"pnl{s}": m["pnl"], f"peak{s}": m["peak"], f"trough{s}": m["trough"], f"dd{s}": m["dd"]})
        out[month] = row
    return out


def test_a_window_drawdown_is_exact_across_month_boundaries():
    """Every month records only its own high, low and drawdown; the window's must still be exact."""
    rng = random.Random(7)
    for _ in range(200):
        months = [f"2026-{m:02d}" for m in range(1, 7)]
        changes = {m: [rng.uniform(-10, 10) for _ in range(rng.randint(0, 8))] for m in months}
        changes = {m: d for m, d in changes.items() if d}  # a month with no trade is absent

        equity, high, worst = 0.0, 0.0, 0.0
        for m in months:
            for d in changes.get(m, []):
                equity += d
                high = max(high, equity)
                worst = max(worst, high - equity)

        stats = window_stats(periods_from(changes), months)
        assert stats["pnl5"] == pytest.approx(equity)
        assert stats["dd5"] == pytest.approx(worst)


def test_only_complete_jst_months_count():
    r = DtRange.from_strings("2025-07-01", "2026-09-01")
    months = complete_months(r.start_ts, r.end_ts)
    assert months[0] == "2025-07" and months[-1] == "2026-08" and len(months) == 14
    r = DtRange.from_strings("2025-07-15", "2025-10-31")
    assert complete_months(r.start_ts, r.end_ts) == ["2025-08", "2025-09", "2025-10"]


def test_neighbours_are_one_step_on_numeric_axes_only():
    params = {"schedule": "a", "box": 0.003, "step": 0.05}
    got = {dict(k)["box"] if dict(k)["box"] != 0.003 else ("step", dict(k)["step"]) for k in neighbours(params, GRID)}
    assert got == {0.0025, 0.004, ("step", 0.04)}  # no "schedule" neighbour; 0.05 is the top step


def test_a_set_that_loses_at_pnl10_ranks_below_every_one_that_does_not():
    settings = SelectionSettings(neighbors=False)
    rows = rank([
        ({"x": 1}, {"pnl0": 300.0, "pnl5": 200.0, "pnl10": -1.0, "trades": 10, "dd5": 5.0}),
        ({"x": 2}, {"pnl0": 60.0, "pnl5": 50.0, "pnl10": 40.0, "trades": 10, "dd5": 5.0}),
    ], None, settings)
    assert [r["params"]["x"] for r in rows] == [2, 1]
    assert rows[1]["passes"] is False


def test_drawdown_and_trade_filters():
    stats = {"pnl0": 1.0, "pnl5": 1.0, "pnl10": 1.0, "trades": 5, "dd5": 50.0}
    assert rank([({"x": 1}, stats)], None, SelectionSettings(min_trades=6))[0]["passes"] is False
    assert rank([({"x": 1}, stats)], None, SelectionSettings(max_drawdown=40.0))[0]["passes"] is False
    assert rank([({"x": 1}, stats)], None, SelectionSettings(max_drawdown=60.0, min_trades=5))[0]["passes"] is True


def test_a_lone_peak_loses_to_a_plateau():
    """Smoothed over its grid neighbours, an isolated best does worse than a consistently good region."""
    def stats(p):
        return {"pnl0": p, "pnl5": p, "pnl10": p, "trades": 10, "dd5": 0.0}

    candidates = [
        ({"schedule": "a", "box": 0.0025, "step": 0.04}, stats(10.0)),
        ({"schedule": "a", "box": 0.003, "step": 0.04}, stats(100.0)),   # the lone peak
        ({"schedule": "a", "box": 0.004, "step": 0.04}, stats(10.0)),
        ({"schedule": "b", "box": 0.0025, "step": 0.04}, stats(80.0)),
        ({"schedule": "b", "box": 0.003, "step": 0.04}, stats(90.0)),    # a plateau
        ({"schedule": "b", "box": 0.004, "step": 0.04}, stats(80.0)),
    ]
    ranked = rank(candidates, GRID, SelectionSettings())
    assert ranked[0]["params"]["schedule"] == "b"
    peak = next(r for r in ranked if r["params"] == {"schedule": "a", "box": 0.003, "step": 0.04})
    assert peak["score"] == pytest.approx(40.0)   # (100 + 10 + 10) / 3
    plain = rank(candidates, GRID, SelectionSettings(neighbors=False))[0]
    assert plain["params"]["schedule"] == "a"


def run(params, changes):
    return {"params": params, "result": {"pnl5": 0.0, "periods": periods_from(changes)}}


def test_walk_forward_chooses_on_the_past_and_scores_on_what_follows():
    months = ["2026-01", "2026-02", "2026-03", "2026-04"]
    # "early" wins Jan-Feb then loses; "late" is the opposite.
    early = run({"x": 1}, {"2026-01": [10.0], "2026-02": [10.0], "2026-03": [-5.0], "2026-04": [-5.0]})
    late = run({"x": 2}, {"2026-01": [1.0], "2026-02": [1.0], "2026-03": [8.0], "2026-04": [8.0]})
    settings = SelectionSettings(neighbors=False, train_months=2, test_months=1)

    wf = walk_forward([early, late], months, None, settings)
    assert [(f["test"][0], f["params"]["x"], f["pnl5"]) for f in wf["folds"]] == [
        ("2026-03", 1, -5.0),     # chosen on Jan-Feb: early, which then loses
        ("2026-04", 2, 8.0),      # chosen on Feb-Mar (early 5, late 9): late
    ]
    assert wf["out_of_sample"]["pnl5"] == pytest.approx(3.0)
    assert wf["out_of_sample"]["profitable_folds"] == 1
    assert wf["latest"][0]["params"] == {"x": 2}   # chosen on Mar-Apr


def test_select_falls_back_to_the_whole_range_and_says_why():
    old_log = {"params": {"x": 1}, "result": {"pnl0": 3.0, "pnl5": 2.0, "pnl10": 1.0, "pnl_cnt5": 4, "max_drawdown5": 1.0}}
    r = DtRange.from_strings("2025-07-01", "2026-09-01")
    choice = select([old_log], r.start_ts, r.end_ts, None, SelectionSettings())
    assert choice["method"] == "whole range" and "--force" in choice["reason"]
    assert choice["recommended"] == {"x": 1}

    short = DtRange.from_strings("2026-01-01", "2026-03-31")
    with_periods = run({"x": 1}, {"2026-01": [1.0]})
    choice = select([with_periods], short.start_ts, short.end_ts, None, SelectionSettings())
    assert choice["method"] == "whole range" and "complete month" in choice["reason"]


def test_nothing_is_recommended_when_nothing_passes():
    losing = {"params": {"x": 1}, "result": {"pnl0": 1.0, "pnl5": -1.0, "pnl10": -2.0, "pnl_cnt5": 4, "max_drawdown5": 3.0}}
    r = DtRange.from_strings("2026-01-01", "2026-01-31")
    assert select([losing], r.start_ts, r.end_ts, None, SelectionSettings())["recommended"] is None


def test_selection_settings_are_checked():
    with pytest.raises(ValueError):
        SelectionSettings(test_months=0)
