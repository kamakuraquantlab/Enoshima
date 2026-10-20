"""Recommendations per range and per group of parameter sets."""

import json

from qate.util.dt_range import DtRange

from enoshima import best, run_dir
from enoshima.ledger import Trade
from enoshima.selection import SelectionSettings

JULY = DtRange.from_strings("2026-07-01", "2026-07-31")
AUGUST = DtRange.from_strings("2026-08-01", "2026-08-31")


def write_run(root, run_id, params, dt_range, pnl, strategy="s.v1", temporary=False, rev=None):
    """A finished run folder whose one trade made `pnl` at every slippage."""
    ts = dt_range.start_ts + 3600
    trades = [Trade(ts, ts - 60, "X", "Y", "long", 1.0, 1.0, pnl, 0.0, pnl, pnl, pnl)]
    meta = {"params": params, "strategy": strategy, "start_str": dt_range.start_str, "end_str": dt_range.end_str,
            "is_temporary_run": temporary, "strategy_rev": rev}
    summary = {"pnl0": pnl, "pnl5": pnl, "pnl10": pnl, "pnl_cnt5": 1, "max_drawdown5": 0.0}
    run_dir.write(root, run_id, meta, summary, trades)


def test_each_range_and_each_categorical_value_gets_its_own_recommendation(tmp_path):
    for mode, box, pnl in [("a", 1, 10.0), ("a", 2, 30.0), ("b", 1, 50.0), ("b", 2, 20.0)]:
        write_run(tmp_path, f"jul{mode}{box}", {"mode": mode, "box": box}, JULY, pnl)
    write_run(tmp_path, "aug", {"mode": "a", "box": 1}, AUGUST, 5.0)
    write_run(tmp_path, "other", {"mode": "a", "box": 1}, JULY, 99.0, strategy="s.v2")
    write_run(tmp_path, "tmp_x", {"mode": "a", "box": 9}, JULY, 999.0, temporary=True)   # never a candidate

    groups = best.recommend(tmp_path, 7, SelectionSettings(neighbors=False))
    got = {(g["strategy"], g["start_str"][:7], g["group"]["mode"]): (g["runs"], g["recommended_run_id"]) for g in groups}
    assert got == {
        ("s.v1", "2026-07", "a"): (2, "jula2"),
        ("s.v1", "2026-07", "b"): (2, "julb1"),   # the best of "b", though "a" holds the best overall pnl
        ("s.v1", "2026-08", "a"): (1, "aug"),
        ("s.v2", "2026-07", "a"): (1, "other"),   # another variant is not a candidate beside s.v1
    }

    out = tmp_path / "best_result" / "7"
    assert sorted(p.name for p in out.iterdir()) == sorted(
        ["selection.json", "jula2.json", "julb1.json", "aug.json", "other.json"])
    assert len(json.loads((out / "selection.json").read_text())["groups"]) == 4


def test_group_by_names_the_split_and_an_empty_list_is_one_group(tmp_path):
    for mode, box, pnl in [("a", 1, 10.0), ("b", 1, 50.0), ("a", 2, 30.0), ("b", 2, 20.0)]:
        write_run(tmp_path, f"{mode}{box}", {"mode": mode, "box": box}, JULY, pnl)

    [one] = best.recommend(tmp_path, 1, SelectionSettings(neighbors=False, group_by=[]))
    assert one["group"] == {} and one["runs"] == 4 and one["recommended_run_id"] == "b1"

    by_box = best.recommend(tmp_path, 2, SelectionSettings(neighbors=False, group_by=["box"]))
    assert {g["group"]["box"]: g["recommended_run_id"] for g in by_box} == {1: "b1", 2: "a2"}


def test_neighbours_come_from_the_group_only(tmp_path):
    """A set's neighbours share its categorical values: another mode's runs never smooth it."""
    runs = [{"params": {"mode": m, "box": b}} for m in ("a", "b") for b in (1, 2, 3)]
    assert best.categorical(runs) == ["mode"]
    axes = best.axes([r for r in runs if r["params"]["mode"] == "a"])
    assert axes == [{"name": "mode", "values": ["a"]}, {"name": "box", "values": [1, 2, 3]}]


def test_a_group_where_nothing_passes_recommends_nothing(tmp_path):
    write_run(tmp_path, "lose", {"mode": "a", "box": 1}, JULY, -1.0)
    write_run(tmp_path, "win", {"mode": "b", "box": 1}, JULY, 1.0)
    groups = {g["group"]["mode"]: g for g in best.recommend(tmp_path, 3, SelectionSettings())}
    assert groups["a"]["recommended_run_id"] is None and groups["a"]["recommended"] is None
    assert groups["b"]["recommended_run_id"] == "win"
    assert sorted(p.name for p in (tmp_path / "best_result" / "3").iterdir()) == ["selection.json", "win.json"]


def test_runs_of_two_revisions_of_a_strategy_are_not_ranked_together(tmp_path):
    write_run(tmp_path, "old", {"box": 1}, JULY, 99.0, rev="aaaa")
    write_run(tmp_path, "new1", {"box": 1}, JULY, 10.0, rev="bbbb")
    write_run(tmp_path, "new2", {"box": 2}, JULY, 20.0, rev="bbbb")

    groups = best.recommend(tmp_path, 7, SelectionSettings(neighbors=False))
    assert {g["strategy"]: g["runs"] for g in groups} == {"s.v1@aaaa": 1, "s.v1@bbbb": 2}
