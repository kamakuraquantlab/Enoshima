"""The selection rule's own parameters, tested: window, history and how often it chooses again.

`selection.walk_forward` tests the rule `--optimize` uses, a month at a time.
That rule has parameters of its own, and this asks whether others would have
done better. A rule is

- `window`: `"expanding"` chooses on every day since the range began,
  `"rolling"` on the latest `history`;
- `history`: for a rolling window its length, cut short at the range start;
  for an expanding one only the history the first test needs, as
  `train_months` is in `selection`;
- `every`: how often the choice is made again -- `"1d"`, `"2w"`, `"1m"` -- or
  `"never"`, chosen once at the start of the test and held to its end.

Spans are whole JST days (`d`), weeks of seven days (`w`) or calendar months
(`m`). The choice itself is `selection`'s, unchanged: the filters, the score
and grid-neighbour smoothing of `enoshima.json`'s `"selection"`, on the
figures of the training days, per group as `best` groups (`group_by`). Each
fold then trades the chosen set over the days until the next choice; a fold
where nothing passes the filters sits out, at zero.

**Every rule is tested on the same days**, from `test_from` to the end of the
range, so their totals compare; `test_from` defaults to the range start plus
the longest `history` asked for. Beside the rules, `hindsight` is the set
chosen on the whole range, scored on those same days: it has seen them, so it
is a ceiling, not a rule.

A day's figures come from the run folder's `by_day.json`, whose rows combine
exactly (see `ledger`), so a fold's drawdown, and the out-of-sample curve's
across folds that changed set, are exact. `top_share5` uses `trades.csv`.

The caution that applies to a parameter set applies here one level up: of many
rules tried on the same test days, the one with the best total is partly the
luckiest. Prefer a rule that holds up across groups and environments to one
that wins one, and the simpler of two that are close.
"""

import datetime as dt
import itertools
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from logging import getLogger
from pathlib import Path

from qate.util.dt_range import DtRange

from .. import best, selection
from ..ledger import JST

LOG = getLogger(__name__)

OUTPUT_DIR = "post_analysis"
NEVER = "never"

_SPAN = re.compile(r"^([1-9][0-9]*)([dwm])$")


def parse_span(text: str) -> tuple[int, str]:
    """`"6m"` -> `(6, "m")`: a number of days, weeks or calendar months."""
    m = _SPAN.match(text.strip())
    if not m:
        raise ValueError(f"{text!r} is not a span such as 1d, 2w or 6m")
    return int(m.group(1)), m.group(2)


def advance(day: dt.date, span: tuple[int, str], back: bool = False) -> dt.date:
    n, unit = span
    n = -n if back else n
    if unit == "d":
        return day + dt.timedelta(days=n)
    if unit == "w":
        return day + dt.timedelta(weeks=n)
    month = day.year * 12 + day.month - 1 + n
    year, month = divmod(month, 12)
    # A month later than the 31st is its last day.
    last = (dt.date(year + (month + 1) // 12, (month + 1) % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month + 1, min(day.day, last))


def days(start: dt.date, stop: dt.date) -> list[str]:
    """`by_day.json` keys of `start` up to, not including, `stop`."""
    return [(start + dt.timedelta(days=i)).isoformat() for i in range((stop - start).days)]


def complete_days(start_ts: float, end_ts: float) -> tuple[dt.date, dt.date]:
    """The first JST day wholly inside the range, and the day after the last."""
    start = dt.datetime.fromtimestamp(start_ts, JST)
    first = start.date() if start.time() == dt.time() else start.date() + dt.timedelta(days=1)
    end = dt.datetime.fromtimestamp(end_ts, JST)
    stop = end.date() + dt.timedelta(days=1)
    if dt.datetime.combine(stop, dt.time(), JST) - dt.timedelta(microseconds=1) > end:
        stop = end.date()
    return first, stop


@dataclass(frozen=True)
class Rule:
    window: str = "expanding"
    history: str = "6m"
    every: str = "1m"

    def __post_init__(self):
        if self.window not in selection.WINDOWS:
            raise ValueError(f"window {self.window!r} is not one of {selection.WINDOWS}")
        parse_span(self.history)
        if self.every != NEVER:
            parse_span(self.every)

    @property
    def label(self) -> str:
        window = "expanding" if self.window == "expanding" else f"rolling {self.history}"
        return f"{window}, every {self.every}" if self.every != NEVER else f"{window}, once"


def rules(windows: list[str], histories: list[str], everies: list[str]) -> list[Rule]:
    """Every combination, once: an expanding window's history changes nothing once `test_from` is
    fixed, so it takes the longest."""
    longest = max(histories, key=lambda h: advance(dt.date(2000, 1, 1), parse_span(h)))
    out = []
    for w, h, e in itertools.product(windows, histories, everies):
        rule = Rule(w, h if w == "rolling" else longest, e)
        if rule not in out:
            out.append(rule)
    return out


def default_test_from(first: dt.date, rule_list: list[Rule]) -> dt.date:
    return max(advance(first, parse_span(r.history)) for r in rule_list)


# ------------------------------------------------------------------ choosing


def choose(runs: list[dict], keys: list[str], grid: list[dict], settings: selection.SelectionSettings) -> dict | None:
    """The run `selection` would choose on the days `keys`, or None when no set passes."""
    ranked = selection.rank(
        [(r["params"], selection.window_stats(r["result"]["periods"], keys, r["result"]["trade_pnl5"],
                                              settings.top_share)) for r in runs],
        grid, settings)
    if not ranked or not ranked[0]["passes"]:
        return None
    return next(r for r in runs if r["params"] == ranked[0]["params"])


def _curve(folds: list[tuple[dict | None, list[str]]]) -> dict:
    """The out-of-sample figures of trading each fold's run over its days: PnL, and an exact dd5."""
    periods, keys = {}, []
    for run, fold_keys in folds:
        keys.extend(fold_keys)
        if run is not None:
            periods.update((k, run["result"]["periods"][k]) for k in fold_keys if k in run["result"]["periods"])
    stats = selection.window_stats(periods, keys)
    return {k: stats[k] for k in ("pnl0", "pnl5", "pnl10", "dd5", "trades")}


def walk(runs: list[dict], grid: list[dict], settings: selection.SelectionSettings,
         first: dt.date, stop: dt.date, test_from: dt.date, rule: Rule, cache: dict | None = None) -> dict:
    """One rule over one group: the folds, and the out-of-sample totals over [test_from, stop).

    `cache` keeps each choice by its training days, so rules that share them --
    an expanding window chosen daily and monthly -- rank each window once.
    """
    cache = {} if cache is None else cache
    history = parse_span(rule.history)
    folds, traded = [], []
    start = test_from
    while start < stop:
        end = stop if rule.every == NEVER else min(stop, advance(start, parse_span(rule.every)))
        train_from = first if rule.window == "expanding" else max(first, advance(start, history, back=True))
        if (train_from, start) not in cache:
            cache[train_from, start] = choose(runs, days(train_from, start), grid, settings)
        run = cache[train_from, start]
        test_keys = days(start, end)
        tested = _curve([(run, test_keys)])
        folds.append({"train": [train_from.isoformat(), (start - dt.timedelta(days=1)).isoformat()],
                      "test": [start.isoformat(), (end - dt.timedelta(days=1)).isoformat()],
                      "params": run["params"] if run else None, "run_id": run["run_id"] if run else None,
                      **{k: tested[k] for k in ("pnl0", "pnl5", "pnl10")}})
        traded.append((run, test_keys))
        start = end

    ids = [f["run_id"] for f in folds]
    return {
        "rule": asdict(rule),
        "label": rule.label,
        "folds": folds,
        "out_of_sample": {
            **_curve(traded),
            "folds": len(folds),
            "profitable_folds": sum(1 for f in folds if f["params"] is not None and f["pnl5"] > 0),
            "sat_out": sum(1 for f in folds if f["params"] is None),
            "switches": sum(1 for a, b in zip(ids, ids[1:]) if a != b),
        },
    }


def hindsight(runs: list[dict], grid: list[dict], settings: selection.SelectionSettings,
              first: dt.date, stop: dt.date, test_from: dt.date) -> dict:
    """The set chosen on the whole range, scored on the test days it has already seen."""
    run = choose(runs, days(first, stop), grid, settings)
    return {"label": "hindsight", "params": run["params"] if run else None,
            "run_id": run["run_id"] if run else None,
            "out_of_sample": {**_curve([(run, days(test_from, stop))]), "folds": 1, "profitable_folds": None,
                              "sat_out": int(run is None), "switches": 0}}


# ------------------------------------------------------------------ the report


def analyse(root: Path, rule_list: list[Rule], settings: selection.SelectionSettings,
            test_from: dt.date | None = None) -> list[dict]:
    """Every rule, and hindsight, for every range and group under `root`."""
    runs = best.load_runs(root, "day")
    tasks = []
    for (strategy, start_str, end_str), groups in sorted(best.group(runs, settings.group_by).items(),
                                                         key=lambda kv: tuple(str(k) for k in kv[0])):
        dt_range = DtRange.from_strings(start_str, end_str)
        first, stop = complete_days(dt_range.start_ts, dt_range.end_ts)
        tf = test_from or default_test_from(first, rule_list)
        if not first < tf < stop:
            LOG.warning(f"{strategy} {start_str}..{end_str}: test from {tf} leaves no training or no test days; skipped")
            continue
        for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
            tasks.append((strategy, start_str, end_str, key, members, first, stop, tf))

    def one(task) -> dict:
        strategy, start_str, end_str, key, members, first, stop, tf = task
        grid, cache = best.axes(members), {}
        return {
            "strategy": strategy, "start_str": start_str, "end_str": end_str,
            "group": dict(key), "runs": len(members),
            "test": [tf.isoformat(), (stop - dt.timedelta(days=1)).isoformat()],
            "rules": [walk(members, grid, settings, first, stop, tf, r, cache) for r in rule_list],
            "hindsight": hindsight(members, grid, settings, first, stop, tf),
        }

    # A group's runs are its own, so groups share nothing and run side by side.
    with ThreadPoolExecutor(max_workers=max(1, min(len(tasks), os.cpu_count() or 1)),
                            thread_name_prefix="walk-forward-") as executor:
        return list(executor.map(one, tasks))


def _group_name(group: dict) -> str:
    return " ".join(str(v) for v in group.values()) or "all"


def sums(groups: list[dict], names: list[list[str]]) -> list[dict]:
    """Out-of-sample PnL of trading several groups side by side, e.g. two schedules that split the day.

    Each entry of `names` lists group names (`_group_name`, the values joined)
    whose totals are added, per range and rule. No drawdown: two groups' trades
    interleave within a day, and a day's rows do not say how.
    """
    out = []
    by_range: dict[tuple, dict[str, dict]] = {}
    for g in groups:
        by_range.setdefault((g["strategy"], g["start_str"], g["end_str"]), {})[_group_name(g["group"])] = g
    for (strategy, start_str, end_str), named in by_range.items():
        for parts in names:
            missing = [p for p in parts if p not in named]
            if missing:
                LOG.warning(f"{strategy} {start_str}..{end_str}: no group named {missing} to sum; "
                            f"there are {sorted(named)}")
                continue
            members = [named[p] for p in parts]
            rows = []
            for i, rule in enumerate(members[0]["rules"]):
                rows.append({"label": rule["label"], **{
                    f"pnl{s}": sum(m["rules"][i]["out_of_sample"][f"pnl{s}"] for m in members)
                    for s in selection.SLIPPAGES}})
            rows.append({"label": "hindsight", **{
                f"pnl{s}": sum(m["hindsight"]["out_of_sample"][f"pnl{s}"] for m in members)
                for s in selection.SLIPPAGES}})
            out.append({"strategy": strategy, "start_str": start_str, "end_str": end_str,
                        "name": " + ".join(parts), "rows": rows})
    return out


def table(groups: list[dict], summed: list[dict]) -> list[str]:
    def k(v):
        return "n/a" if v is None else f"{v / 1000:+.1f}k"

    lines = []
    head = f"  {'rule':28s} {'pnl5':>9s} {'pnl10':>9s} {'dd5':>8s} {'profitable':>11s} {'switches':>8s} {'sat out':>7s}"
    for g in groups:
        lines.append(f"{g['strategy']} {g['start_str']}..{g['end_str']} {_group_name(g['group'])} "
                     f"({g['runs']} runs), tested {g['test'][0]}..{g['test'][1]}")
        lines.append(head)
        for r in [*g["rules"], g["hindsight"]]:
            o = r["out_of_sample"]
            profitable = "" if o["profitable_folds"] is None else f"{o['profitable_folds']}/{o['folds']}"
            lines.append(f"  {r['label']:28s} {k(o['pnl5']):>9s} {k(o['pnl10']):>9s} {o['dd5'] / 1000:7.1f}k "
                         f"{profitable:>11s} {o['switches']:8d} {o['sat_out']:7d}")
    for s in summed:
        lines.append(f"{s['strategy']} {s['start_str']}..{s['end_str']} {s['name']}")
        lines.append(f"  {'rule':28s} {'pnl5':>9s} {'pnl10':>9s}")
        for r in s["rows"]:
            lines.append(f"  {r['label']:28s} {k(r['pnl5']):>9s} {k(r['pnl10']):>9s}")
    return lines


def run(root: Path, rule_list: list[Rule], settings: selection.SelectionSettings,
        test_from: dt.date | None = None, sum_names: list[list[str]] | None = None) -> Path | None:
    """Analyse, log the table, and write `post_analysis/walk_forward_<stamp>.{json,txt}`; return the JSON's path."""
    groups = analyse(root, rule_list, settings, test_from)
    if not groups:
        return None
    summed = sums(groups, sum_names or [])
    lines = table(groups, summed)
    for line in lines:
        LOG.info(line)

    out_dir = Path(root) / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    while (out_dir / f"walk_forward_{stamp}.json").exists():
        stamp += 1
    path = out_dir / f"walk_forward_{stamp}.json"
    path.write_text(json.dumps({"settings": asdict(settings), "rules": [asdict(r) for r in rule_list],
                                "groups": groups, "sums": summed}, indent=2))
    path.with_suffix(".txt").write_text("\n".join(lines) + "\n")
    LOG.info(f"Saved {path}")
    return path
