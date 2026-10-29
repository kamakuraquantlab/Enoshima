"""Walk-forward over the selection rule's own parameters -- window, history, reselection interval -- and training it."""

import csv
import datetime as dt
import json

import numpy as np
import pytest
from qate.util.dt_range import DtRange

from enoshima import selection
from enoshima.lake import WHOLE, Store
from enoshima.ledger import JST, Trade, aggregate
from enoshima.post_analysis import walk_forward as wf
from enoshima.selection import SelectionSettings
from enoshima.settings import load_settings

PNL5 = SelectionSettings(score="pnl5", neighbors=False)


def at(day: str, hour: int = 0) -> dt.datetime:
    return dt.datetime.fromisoformat(day).replace(hour=hour, tzinfo=JST)


def trade(day: str, pnl: float, hour: int = 12, minute: int = 0) -> Trade:
    ts = at(day, hour).timestamp() + minute * 60
    return Trade(ts, ts - 60, "X", "Y", "long", 1.0, 1.0, pnl, 0.0, pnl, pnl, pnl)


def days(start: str, n: int) -> list[str]:
    return [(dt.date.fromisoformat(start) + dt.timedelta(days=i)).isoformat() for i in range(n)]


def run(run_id: str, params: dict, changes: dict[str, list[float]]) -> dict:
    """A loaded run, as `walk_forward` reads it, from each day's PnL changes (one trade an hour from 01:00)."""
    trades = [trade(day, c, hour=1 + i) for day, cs in sorted(changes.items()) for i, c in enumerate(cs)]
    return {"run_id": run_id, "params": params, "series": wf.Series.of(trades)}


def hourly(run_id: str, params: dict, changes: dict[tuple[str, int], float]) -> dict:
    """A loaded run from PnL changes by (day, hour)."""
    trades = [trade(day, c, hour=h, minute=30) for (day, h), c in sorted(changes.items())]
    return {"run_id": run_id, "params": params, "series": wf.Series.of(trades)}


def test_spans_and_calendar_steps():
    assert wf.parse_span("6m") == (6, "m") and wf.parse_span("2w") == (2, "w") and wf.parse_span("4h") == (4, "h")
    assert wf.span_hours("4h") == 4 and wf.span_hours("1d") == 24 and wf.span_hours("1m") == 720
    for bad in ("0d", "m", "1y", "1.5m", "30min"):
        with pytest.raises(ValueError):
            wf.parse_span(bad)
    assert wf.advance(at("2026-01-31", 9), (1, "m")) == at("2026-02-28", 9)   # the month's last day
    assert wf.advance(at("2026-03-31"), (1, "m"), back=True) == at("2026-02-28")
    assert wf.advance(at("2025-12-15"), (2, "m")) == at("2026-02-15")
    assert wf.advance(at("2026-01-01"), (2, "w")) == at("2026-01-15")
    assert wf.advance(at("2026-01-01", 22), (4, "h")) == at("2026-01-02", 2)
    with pytest.raises(ValueError):
        wf.Rule(every="sometimes")


def test_only_complete_jst_hours_count():
    r = DtRange.from_strings("2026-01-01", "2026-01-31")
    assert wf.complete_hours(r.start_ts, r.end_ts) == (at("2026-01-01"), at("2026-02-01"))
    partial = DtRange.from_strings("20260101_0930", "20260131_1200")
    assert wf.complete_hours(partial.start_ts, partial.end_ts) == (at("2026-01-01", 10), at("2026-01-31", 12))


def test_each_history_takes_its_own_intervals():
    got = wf.rules(["rolling"], {"3d": ["4h", "1d"], "2m": ["1w"]})
    assert [r.key for r in got] == ["rolling_3d_4h", "rolling_3d_1d", "rolling_2m_1w"]
    assert wf.default_test_from(at("2026-01-01"), got) == at("2026-03-01")
    assert wf.default_test_from(at("2026-01-01"), got, earliest=True) == at("2026-01-04")


def test_on_common_hours_an_expanding_rule_takes_one_history_and_combinations_are_unique():
    got = wf.on_common_hours(wf.rules(["expanding", "rolling"], {"1m": ["1w", "never"], "3m": ["1w", "never"]}))
    assert [r.label for r in got] == ["expanding, every 1w", "expanding, once",
                                      "rolling 1m, every 1w", "rolling 1m, once",
                                      "rolling 3m, every 1w", "rolling 3m, once"]
    assert all(r.history == "3m" for r in got if r.window == "expanding")
    assert wf.default_test_from(at("2026-01-01"), got) == at("2026-04-01")
    assert [r.key for r in got[:3]] == ["expanding_1w", "expanding_never", "rolling_1m_1w"]
    assert wf.Rule("rolling", "24h", "4h").params == {"window": "rolling", "history_h": 24, "every_h": 4}


def test_a_window_has_the_figures_selection_computes():
    changes = {"2026-01-01": [3.0, -5.0, 1.0], "2026-01-02": [-2.0, 7.0], "2026-01-03": [4.0, -9.0, 2.0]}
    r = run("r", {}, changes)
    trades = [trade(d, c, hour=1 + i) for d, cs in sorted(changes.items()) for i, c in enumerate(cs)]
    expected = selection.window_stats(aggregate(trades, "day"), sorted(changes), changes, 0.3)
    got = r["series"].stats(at("2026-01-01"), at("2026-01-04"), 0.3)
    assert got == pytest.approx(expected)
    # A window inside the range: the drawdown is the window's own, from zero.
    assert r["series"].stats(at("2026-01-02"), at("2026-01-03"))["dd5"] == pytest.approx(2.0)
    assert r["series"].stats(at("2026-01-01", 2), at("2026-01-01", 3))["pnl5"] == pytest.approx(-5.0)


def test_each_fold_chooses_on_the_days_before_it_and_trades_until_the_next_choice():
    # "early" wins the first week and loses after; "late" the opposite.
    first, stop = at("2026-01-01"), at("2026-01-22")
    early = run("e", {"x": 1}, {**{d: [10.0] for d in days("2026-01-01", 7)}, **{d: [-1.0] for d in days("2026-01-08", 14)}})
    late = run("l", {"x": 2}, {**{d: [1.0] for d in days("2026-01-01", 7)}, **{d: [5.0] for d in days("2026-01-08", 14)}})
    test_from = at("2026-01-08")

    weekly = wf.walk([early, late], [], PNL5, first, stop, test_from, wf.Rule("rolling", "1w", "1w"))
    assert [(f["train"], f["test"], f["run_id"]) for f in weekly["folds"]] == [
        (["2026-01-01 00:00", "2026-01-08 00:00"], ["2026-01-08 00:00", "2026-01-15 00:00"], "e"),  # early led its week
        (["2026-01-08 00:00", "2026-01-15 00:00"], ["2026-01-15 00:00", "2026-01-22 00:00"], "l"),  # late the next
    ]
    assert weekly["out_of_sample"]["pnl5"] == pytest.approx(-7.0 + 35.0)
    assert weekly["out_of_sample"]["switches"] == 1

    # Expanding over both weeks, early still leads on 2026-01-15: 70 - 7 against 7 + 35.
    expanding = wf.walk([early, late], [], PNL5, first, stop, test_from, wf.Rule("expanding", "1w", "1w"))
    assert [f["run_id"] for f in expanding["folds"]] == ["e", "e"]
    assert [f["train"][0] for f in expanding["folds"]] == ["2026-01-01 00:00", "2026-01-01 00:00"]

    once = wf.walk([early, late], [], PNL5, first, stop, test_from, wf.Rule("rolling", "1w", "never"))
    assert [f["test"] for f in once["folds"]] == [["2026-01-08 00:00", "2026-01-22 00:00"]]
    assert once["out_of_sample"]["pnl5"] == pytest.approx(-14.0)


def test_a_rule_can_choose_again_every_few_hours():
    # "am" earns in the morning and loses in the afternoon; "pm" the opposite.
    am = hourly("am", {"x": 1}, {(d, h): (2.0 if h < 12 else -1.0) for d in days("2026-01-01", 3) for h in range(24)})
    pm = hourly("pm", {"x": 2}, {(d, h): (-1.0 if h < 12 else 2.0) for d in days("2026-01-01", 3) for h in range(24)})
    first, stop, test_from = at("2026-01-01"), at("2026-01-04"), at("2026-01-02")
    # On the 12 hours before, the half-day just gone looks best; it is the wrong one each time.
    got = wf.walk([am, pm], [], PNL5, first, stop, test_from, wf.Rule("rolling", "12h", "12h"))
    assert [f["run_id"] for f in got["folds"]] == ["pm", "am", "pm", "am"]
    assert got["out_of_sample"]["pnl5"] == pytest.approx(4 * 12 * -1.0)
    # The same 24 hours before chooses on a tie, held for a day: a day's -12 + 24 each.
    daily = wf.walk([am, pm], [], PNL5, first, stop, test_from, wf.Rule("rolling", "24h", "24h"))
    assert daily["out_of_sample"]["pnl5"] == pytest.approx(2 * 12.0)


def test_the_out_of_sample_drawdown_is_exact_across_a_change_of_set():
    first, stop, test_from = at("2026-01-01"), at("2026-01-04"), at("2026-01-02")
    a = run("a", {"x": 1}, {"2026-01-01": [5.0], "2026-01-02": [4.0, -6.0], "2026-01-03": [9.0]})
    b = run("b", {"x": 2}, {"2026-01-01": [1.0], "2026-01-02": [6.0], "2026-01-03": [3.0, -8.0]})
    # Daily: 01-02 on a (5 against 1), 01-03 on b (7 against 3).
    got = wf.walk([a, b], [], PNL5, first, stop, test_from, wf.Rule("expanding", "1d", "1d"))
    assert [f["run_id"] for f in got["folds"]] == ["a", "b"]
    # The curve: +4, -6 (from a), then +3, -8 (from b): high 4, low -7.
    assert got["out_of_sample"]["pnl5"] == pytest.approx(-7.0)
    assert got["out_of_sample"]["dd5"] == pytest.approx(11.0)


def test_a_fold_where_nothing_passes_sits_out():
    first, stop, test_from = at("2026-01-01"), at("2026-01-03"), at("2026-01-02")
    losing = run("x", {"x": 1}, {"2026-01-01": [-1.0], "2026-01-02": [5.0]})
    got = wf.walk([losing], [], SelectionSettings(neighbors=False), first, stop, test_from,
                  wf.Rule("expanding", "1d", "1d"))
    assert got["folds"][0]["params"] is None and got["out_of_sample"]["pnl5"] == 0.0
    assert got["out_of_sample"]["sat_out"] == 1


def write_run(store, run_id, params, changes, start="2026-01-01", end="2026-01-21"):
    r = DtRange.from_strings(start, end)
    trades = [trade(day, c, hour=1 + i) for day, cs in sorted(changes.items()) for i, c in enumerate(cs)]
    meta = {"params": params, "strategy": "s.v1", "start_str": r.start_str, "end_str": r.end_str}
    total = sum(c for cs in changes.values() for c in cs)
    summary = {"pnl0": total, "pnl5": total, "pnl10": total, "pnl_cnt5": len(trades), "max_drawdown5": 0.0}
    store.write_part(run_id, WHOLE, meta, summary, trades)
    store.write_run(run_id, meta, summary, trades)


def test_the_command_writes_every_group_and_rule_and_the_sums(tmp_path):
    store = Store(tmp_path, "E")
    for mode, box, pnl in [("a", 1, 1.0), ("a", 2, 2.0), ("b", 1, 3.0), ("b", 2, -1.0)]:
        write_run(store, f"{mode}{box}", {"mode": mode, "box": box}, {d: [pnl] for d in days("2026-01-01", 21)})

    rule_list = wf.rules(["expanding"], {"1w": ["1w", "never"]})
    path = wf.run(store, rule_list, SelectionSettings(neighbors=False), sum_names=[["a", "b"]])
    report = json.loads(path.read_text())
    assert path.parent.name == "post_analysis" and path.with_suffix(".txt").is_file()

    groups = {g["group"]["mode"]: g for g in report["groups"]}
    assert set(groups) == {"a", "b"} and groups["a"]["test"] == ["2026-01-08 00:00", "2026-01-22 00:00"]
    assert [r["label"] for r in groups["a"]["rules"]] == ["expanding, every 1w", "expanding, once"]
    assert groups["a"]["hindsight"]["run_id"] == "a2" and groups["b"]["hindsight"]["run_id"] == "b1"
    assert groups["a"]["rules"][0]["out_of_sample"]["pnl5"] == pytest.approx(28.0)   # 14 days of a2

    [summed] = report["sums"]
    assert summed["name"] == "a + b"
    assert [r["pnl5"] for r in summed["rows"]] == pytest.approx([28.0 + 42.0] * 3)


# ------------------------------------------------------------------ training


def test_the_bootstrap_and_the_sign_test():
    rng = np.random.default_rng(1)
    assert wf.block_bootstrap(np.full(30, 1.0) + rng.normal(0, 0.1, 30), 5, 2000, rng) < 0.01
    assert wf.block_bootstrap(rng.normal(0, 1, 60) - 1.0, 5, 2000, rng) > 0.9
    assert wf.block_bootstrap(np.array([1.0]), 5, 100, rng) is None
    assert wf.sign_test(3, 0) == pytest.approx(1 / 8) and wf.sign_test(0, 0) is None
    assert wf.day_edges(at("2026-01-01", 6), at("2026-01-03", 3)) == [
        at("2026-01-01", 6), at("2026-01-02"), at("2026-01-03"), at("2026-01-03", 3)]


def regime_runs() -> list[dict]:
    """Two sets trading hourly, whose lead changes every three days: following the latest hours wins."""
    out = []
    for name, x, sign in (("up", 1, 1.0), ("down", 2, -1.0)):
        regime = {d: 1.0 if (i // 3) % 2 == 0 else -1.0 for i, d in enumerate(days("2026-01-01", 60))}
        out.append(hourly(name, {"x": x}, {(d, h): sign * regime[d] for d in regime for h in range(24)}))
    return out


def test_training_chooses_a_rule_every_month_against_the_baseline():
    runs = regime_runs()
    settings = SelectionSettings(score="pnl5", neighbors=False, positive_pnl10=False)
    s = wf.WalkForwardSettings(window=["rolling"], history={"12h": ["12h", "24h"], "48h": ["12h", "24h"]},
                               train_history="2w", retrain="2w", resamples=500)
    rule_list = s.rules()
    first, stop = at("2026-01-01"), at("2026-03-02")
    inner_from = wf.default_test_from(first, rule_list)
    got = wf.train(runs, {}, settings, s, rule_list, {"x": 1}, first, stop, inner_from)

    assert got["meta"]["baseline"] == {"kind": "default", "run_id": "up", "params": {"x": 1}}
    assert got["meta"]["test"] == ["2026-01-17 00:00", "2026-03-02 00:00"]
    folds = got["folds"]
    assert [f["test"][0] for f in folds] == ["2026-01-17 00:00", "2026-01-31 00:00", "2026-02-14 00:00",
                                             "2026-02-28 00:00"]
    # Choosing on the last 12 hours every 12 is wrong for half a day after each change; every other rule longer.
    assert {f["chosen"] for f in folds} == {"rolling_12h_12h"}
    assert all(not f["fallback"] for f in folds)
    assert {r["key"] for r in folds[0]["ranking"]} == {r.key for r in rule_list} | {"baseline"}

    summary = got["summary"]
    assert summary["trained"]["pnl5"] > 10 * abs(summary["baseline"]["pnl5"])
    assert summary["significance"]["folds_won"] == 4 and summary["significance"]["pnl5"]["p_bootstrap"] < 0.01
    assert summary["current"]["chosen"] == folds[-1]["chosen"]

    # The switches are the parameter sets the trained curve actually held, back to back.
    switches = got["switches"]
    assert switches[0]["from"] == "2026-01-17 00:00" and switches[-1]["to"] == "2026-03-02 00:00"
    assert all(a["to"] == b["from"] for a, b in zip(switches, switches[1:]))
    assert {s["run_id"] for s in switches} == {"up", "down"}
    assert {r["series"] for r in got["curve"]} == {"trained", "baseline", "hindsight", *(r.key for r in rule_list)}


def test_training_starts_each_rule_on_its_own_history_and_ranks_those_that_traded_the_window():
    runs = regime_runs()
    settings = SelectionSettings(score="pnl5", neighbors=False, positive_pnl10=False)
    s = wf.WalkForwardSettings(window=["rolling"], history={"1d": ["12h"], "2w": ["1d"]}, train_window="rolling",
                               train_history="1w", retrain="1w", resamples=200)
    rule_list = s.rules()
    first, stop = at("2026-01-01"), at("2026-03-02")
    got = wf.train(runs, {}, settings, s, rule_list, {"x": 1}, first, stop,
                   wf.default_test_from(first, rule_list, earliest=True))

    # The one-day rule is tested from 01-02 and trained on from 01-09; the two-week one from 01-15.
    assert {r["key"]: r["from"] for r in got["meta"]["rules"]} == {"rolling_1d_12h": "2026-01-02 00:00",
                                                                  "rolling_2w_1d": "2026-01-15 00:00"}
    assert got["meta"]["test"] == ["2026-01-09 00:00", "2026-03-02 00:00"]
    ranked = [{r["key"] for r in f["ranking"]} for f in got["folds"]]
    # Until the two-week rule has a week of its own, it is not ranked.
    assert ranked[:2] == [{"rolling_1d_12h", "baseline"}] * 2
    assert all(k == {"rolling_1d_12h", "rolling_2w_1d", "baseline"} for k in ranked[2:])
    assert [f["train"] for f in got["folds"][:3]] == [["2026-01-02 00:00", "2026-01-09 00:00"],
                                                      ["2026-01-09 00:00", "2026-01-16 00:00"],
                                                      ["2026-01-16 00:00", "2026-01-23 00:00"]]

    # Each run of the group held, against each rule switching, over that rule's own test hours.
    against = got["runs"]
    assert set(against["runs"]) == {"up", "down"}
    for rule in against["rules"]:
        a = at(rule["from"].split()[0])
        assert rule["pnl5"] == pytest.approx(sum(c["pnl5"] for c in got["curve"] if c["series"] == rule["key"]))
        for r in runs:
            assert against["runs"][r["run_id"]]["rules"][rule["key"]]["pnl5"] == pytest.approx(
                r["series"].stats(a, stop)["pnl5"])
    assert against["best"] == max(against["rules"], key=lambda r: r["pnl5"])["key"]
    # A rule's curve starts where it does.
    assert min(c["ts"] for c in got["curve"] if c["series"] == "rolling_2w_1d") >= at("2026-01-15").timestamp()


def test_an_expanding_training_window_starts_with_its_latest_candidate():
    runs = regime_runs()
    s = wf.WalkForwardSettings(window=["rolling"], history={"1d": ["12h"], "2w": ["1d"]}, train_history="1w",
                               retrain="1w", resamples=200)
    rule_list = s.rules()
    first, stop = at("2026-01-01"), at("2026-02-15")
    got = wf.train(runs, {}, SelectionSettings(score="pnl5", neighbors=False, positive_pnl10=False), s, rule_list,
                   {"x": 1}, first, stop, wf.default_test_from(first, rule_list, earliest=True))
    assert [f["train"][0] for f in got["folds"]] == ["2026-01-02 00:00", "2026-01-02 00:00",
                                                     *["2026-01-15 00:00"] * (len(got["folds"]) - 2)]


def test_a_training_fold_where_no_rule_passes_trades_the_baseline():
    losing = run("l", {"x": 1}, {d: [-1.0] for d in days("2026-01-01", 30)})
    s = wf.WalkForwardSettings(window=["rolling"], history={"1d": ["1d"]}, train_history="1w", retrain="1w")
    rule_list = s.rules()
    first, stop = at("2026-01-01"), at("2026-01-31")
    got = wf.train([losing], {}, SelectionSettings(neighbors=False), s, rule_list, {"x": 1}, first, stop,
                   at("2026-01-02"))
    assert all(f["fallback"] and f["chosen"] == "baseline" for f in got["folds"])
    assert got["summary"]["trained"]["pnl5"] == pytest.approx(got["summary"]["baseline"]["pnl5"])


def test_without_a_default_run_the_baseline_is_chosen_once():
    runs = regime_runs()
    s = wf.WalkForwardSettings(window=["rolling"], history={"12h": ["24h"]}, train_history="1w", retrain="1w")
    got = wf.train(runs, {}, SelectionSettings(score="pnl5", neighbors=False, positive_pnl10=False), s,
                   s.rules(), {"x": 99}, at("2026-01-01"), at("2026-01-31"), at("2026-01-02"))
    assert got["meta"]["baseline"]["kind"] == "once" and got["meta"]["baseline"]["run_id"] in ("up", "down")


def test_the_train_command_writes_a_folder_per_group(tmp_path):
    store = Store(tmp_path, "E")
    for mode, box, pnl in [("a", 1, 1.0), ("a", 2, 2.0), ("b", 1, 3.0), ("b", 2, -1.0)]:
        write_run(store, f"{mode}{box}", {"mode": mode, "box": box}, {d: [pnl] for d in days("2026-01-01", 21)})
    s = wf.WalkForwardSettings(window=["expanding"], history={"2d": ["1d", "never"]},
                               train_history="3d", retrain="1w", resamples=200)
    paths = wf.run_train(store, s, SelectionSettings(neighbors=False), {"mode": "a", "box": 1})
    assert len(paths) == 2 and all(p.parent == store.analysis_dir / "post_analysis" / "train" for p in paths)
    assert not list((store.analysis_dir / "post_analysis" / "train").glob("*.tmp-*"))
    by_mode = {}
    for p in paths:
        assert sorted(f.name for f in p.iterdir()) == sorted(wf.TRAIN_FILES)
        meta = json.loads((p / "meta.json").read_text())
        assert meta["train_id"] == p.name and meta["env"] == "E" and meta["runs"] == 2
        assert meta["settings"]["walk_forward"]["retrain"] == "1w"
        by_mode[meta["group"]["mode"]] = (meta, json.loads((p / "summary.json").read_text()), p)

    meta, summary, p = by_mode["a"]
    # The baseline is the default set with the group's own mode: a1 in a, b1 in b.
    assert meta["baseline"]["run_id"] == "a1" and by_mode["b"][0]["baseline"]["run_id"] == "b1"
    assert summary["trained"]["pnl5"] == pytest.approx(2.0 * 16)   # a2 from 01-06 to 01-21
    assert summary["baseline"]["pnl5"] == pytest.approx(1.0 * 16)
    with open(p / "curve.csv") as f:
        rows = list(csv.DictReader(f))
    assert rows[0].keys() == set(wf.CURVE_COLUMNS)
    assert sum(float(r["pnl5"]) for r in rows if r["series"] == "trained") == pytest.approx(summary["trained"]["pnl5"])


def test_walk_forward_settings_come_from_the_env_file(tmp_path):
    assert load_settings(tmp_path).walk_forward == wf.WalkForwardSettings()
    (tmp_path / "enoshima.json").write_text(json.dumps({"walk_forward": {"history": {"24h": ["4h"]}}}))
    got = load_settings(tmp_path).walk_forward
    assert [r.label for r in got.rules()] == ["expanding, every 4h"]
    for bad in ({"histroy": {"1d": ["1d"]}}, {"every": ["4h"]}, {"history": ["1d"]}, {"history": {"1d": []}},
                {"history": {"1d": "4h"}}, {"retrain": "1y"}, {"baseline": "best"},
                {"window": ["rolling"], "history": {"1m": ["1d"], "30d": ["1d"]}}):
        (tmp_path / "enoshima.json").write_text(json.dumps({"walk_forward": bad}))
        with pytest.raises(ValueError):
            load_settings(tmp_path)
