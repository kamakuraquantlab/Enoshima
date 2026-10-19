"""The selection rule's own parameters, tested and trained: window, history and how often it chooses again.

`selection.walk_forward` tests the rule `--optimize` uses, a month at a time.
That rule has parameters of its own, and this asks whether others would have
done better. A rule is

- `window`: `"expanding"` chooses on everything since the range began,
  `"rolling"` on the latest `history`;
- `history`: for a rolling window its length, cut short at the range start;
  for an expanding one only the history the first test needs, as
  `train_months` is in `selection`;
- `every`: how often the choice is made again -- `"4h"`, `"1d"`, `"1m"` -- or
  `"never"`, chosen once at the start of the test and held to its end.

Spans are hours (`h`), JST days (`d`), weeks of seven days (`w`) or calendar
months (`m`), and every boundary falls on a JST hour. The rules to try are
`enoshima.json`'s `"walk_forward"` (`WalkForwardSettings`). The choice itself
is `selection`'s, unchanged: the filters, the score and grid-neighbour
smoothing of `enoshima.json`'s `"selection"`, on the figures of the training
hours, per group as `best` groups (`group_by`). Each fold then trades the
chosen set until the next choice; a fold where nothing passes the filters sits
out, at zero.

**Every rule is tested on the same hours**, from `test_from` to the end of the
range, so their totals compare; `test_from` defaults to the range start plus
the longest `history` asked for. Beside the rules, `hindsight` is the set
chosen on the whole range, scored on those same hours: it has seen them, so it
is a ceiling, not a rule.

A window's figures come from the run folder's `trades.csv`. A trade belongs to
the hour it closed in, as it belongs to a day in `by_day.json`; the drawdown
of a window, and of the out-of-sample curve across folds that changed set, is
computed from the trades in order, so it is exact.

## Training the rule (`--train`)

The table above compares rules on one test span, and the best of them is
partly the luckiest. `train` asks the question a deployment does: had the rule
been chosen every `retrain` (a month) on the `train_history` before, what
would that have earned? Each rule's out-of-sample curve is a candidate,
ranked by `selection.rank` -- the same filters, score and smoothing, with a
rule's history and interval in hours as its grid axes -- and so is the
**baseline**, no switching at all: the parameter set of
`strategy_param_grid.json`'s defaults held throughout (`"default"`), or, when
that set was not run, the set chosen once at the start of the test and held
(`"once"`). A training fold where no candidate passes trades the baseline.

The trained curve is one strategy, chosen without seeing its test hours, so it
can be compared with the baseline directly: the difference of their daily
pnl5 is tested with a moving-block bootstrap (blocks of `block_days`, since a
day's PnL is not independent of the day before's) and the training folds won
with a sign test. Only that comparison is out of sample. Choosing among
several training settings by their p-values is the same fit one level up.

Each group's training is written to `post_analysis/train/<id>/` (`write_training`),
whole or not at all, to be charted.
"""

import calendar
import csv
import datetime as dt
import itertools
import json
import math
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from logging import getLogger
from pathlib import Path

import numpy as np
from qate.util.dt_range import DtRange

from .. import best, selection
from ..ledger import JST

LOG = getLogger(__name__)

OUTPUT_DIR = "post_analysis"
TRAIN_DIR = "train"
NEVER = "never"
BASELINES = ("default", "once")
SLIPPAGES = selection.SLIPPAGES
HOUR = dt.timedelta(hours=1)

# A span's nominal length in hours, for ordering rules on a grid: a month is thirty days.
UNIT_HOURS = {"h": 1, "d": 24, "w": 168, "m": 720}
_SPAN = re.compile(r"^([1-9][0-9]*)([hdwm])$")


def parse_span(text: str) -> tuple[int, str]:
    """`"6m"` -> `(6, "m")`: a number of hours, days, weeks or calendar months."""
    m = _SPAN.match(text.strip())
    if not m:
        raise ValueError(f"{text!r} is not a span such as 4h, 1d, 2w or 6m")
    return int(m.group(1)), m.group(2)


def span_hours(text: str) -> int:
    n, unit = parse_span(text)
    return n * UNIT_HOURS[unit]


def advance(t: dt.datetime, span: tuple[int, str], back: bool = False) -> dt.datetime:
    n, unit = span
    n = -n if back else n
    if unit != "m":
        return t + dt.timedelta(hours=n * UNIT_HOURS[unit])
    year, month = divmod(t.year * 12 + t.month - 1 + n, 12)
    # A month later than the 31st is its last day.
    return t.replace(year=year, month=month + 1, day=min(t.day, calendar.monthrange(year, month + 1)[1]))


def complete_hours(start_ts: float, end_ts: float) -> tuple[dt.datetime, dt.datetime]:
    """The first JST hour wholly inside the range, and the hour after the last."""
    start = dt.datetime.fromtimestamp(start_ts, JST)
    first = start.replace(minute=0, second=0, microsecond=0)
    if first < start:
        first += HOUR
    end = dt.datetime.fromtimestamp(end_ts, JST)
    stop = end.replace(minute=0, second=0, microsecond=0) + HOUR
    if stop - dt.timedelta(microseconds=1) > end:
        stop -= HOUR
    return first, stop


def stamp(t: dt.datetime) -> str:
    """A boundary as the reports write it: JST, to the hour. Spans are [from, to)."""
    return t.strftime("%Y-%m-%d %H:%M")


def parse_time(text: str) -> dt.datetime:
    """`2026-06-01`, `20260601` or `20260601_0900`, as a JST datetime."""
    return dt.datetime.fromtimestamp(DtRange.from_strings(text, text).start_ts, JST)


# ------------------------------------------------------------------ settings


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

    @property
    def key(self) -> str:
        """A name without spaces, for a column or a file."""
        window = "expanding" if self.window == "expanding" else f"rolling_{self.history}"
        return f"{window}_{self.every}"

    @property
    def params(self) -> dict:
        """The rule as `selection.rank` sees a candidate: history and interval in hours are its grid axes."""
        return {"window": self.window, "history_h": span_hours(self.history),
                "every_h": None if self.every == NEVER else span_hours(self.every)}


def rules(windows: list[str], histories: list[str], everies: list[str]) -> list[Rule]:
    """Every combination, once: an expanding window's history changes nothing once `test_from` is
    fixed, so it takes the longest."""
    longest = max(histories, key=lambda h: advance(dt.datetime(2000, 1, 1, tzinfo=JST), parse_span(h)))
    out = []
    for w, h, e in itertools.product(windows, histories, everies):
        rule = Rule(w, h if w == "rolling" else longest, e)
        if rule not in out:
            out.append(rule)
    return out


def default_test_from(first: dt.datetime, rule_list: list[Rule]) -> dt.datetime:
    return max(advance(first, parse_span(r.history)) for r in rule_list)


@dataclass
class WalkForwardSettings:
    """`enoshima.json`'s `"walk_forward"`. Every key optional.

    `window`, `history` and `every` are the rules to try, every combination of
    them. `test_from` is the first test hour, by default the range start plus
    the longest history. The rest is `--train`'s: a rule is chosen every
    `retrain` on the `train_history` before (`train_window` as `window` is for
    a rule), against `baseline`; `block_days` and `resamples` are the bootstrap's.
    """

    window: list[str] = field(default_factory=lambda: ["expanding"])
    history: list[str] = field(default_factory=lambda: ["6m"])
    every: list[str] = field(default_factory=lambda: ["1m", NEVER])
    test_from: str | None = None
    train_window: str = "expanding"
    train_history: str = "1m"
    retrain: str = "1m"
    baseline: str = "default"
    block_days: int = 5
    resamples: int = 10000

    def __post_init__(self):
        for name in ("window", "history", "every"):
            value = getattr(self, name)
            if not (isinstance(value, list) and value and all(isinstance(v, str) for v in value)):
                raise ValueError(f"walk_forward {name} is a non-empty list of strings")
        seen = {}
        for rule in self.rules():
            other = seen.setdefault(tuple(sorted(rule.params.items(), key=str)), rule)
            if other != rule:
                raise ValueError(f"walk_forward rules {other.label!r} and {rule.label!r} have the same length "
                                 f"in hours; list one of them")
        if self.test_from is not None:
            parse_time(self.test_from)
        if self.train_window not in selection.WINDOWS:
            raise ValueError(f"walk_forward train_window {self.train_window!r} is not one of {selection.WINDOWS}")
        parse_span(self.train_history)
        parse_span(self.retrain)
        if self.baseline not in BASELINES:
            raise ValueError(f"walk_forward baseline {self.baseline!r} is not one of {BASELINES}")
        if self.block_days < 1 or self.resamples < 1:
            raise ValueError("walk_forward block_days and resamples are at least 1")

    def rules(self) -> list[Rule]:
        return rules(self.window, self.history, self.every)


# ------------------------------------------------------------------ a curve


def top_share(changes: np.ndarray, share: float) -> float:
    """`selection.top_share`, on an array."""
    gross = float(changes[changes > 0].sum())
    if gross <= 0:
        return 1.0
    k = math.ceil(share * len(changes))
    top = np.partition(changes, len(changes) - k)[len(changes) - k:]
    return float(top[top > 0].sum()) / gross


class Series:
    """Closed trades in time order, as arrays: a run's, or a rule's as it traded one run after another.

    Any window's figures are then a search and a slice: the PnL from prefix
    sums, the drawdown and `top_share5` from the window's trades.
    """

    def __init__(self, ts: np.ndarray, changes: dict[str, np.ndarray]):
        self.ts = ts
        self.changes = changes
        self._cum = {s: np.concatenate(([0.0], np.cumsum(c))) for s, c in changes.items()}

    @classmethod
    def of(cls, trades) -> "Series":
        trades = sorted(trades, key=lambda t: t.close_ts)
        return cls(np.array([t.close_ts for t in trades], dtype=float),
                   {s: np.array([getattr(t, f"pnl_change{s}") for t in trades], dtype=float) for s in SLIPPAGES})

    @classmethod
    def concat(cls, parts: list["Series"]) -> "Series":
        if not parts:
            return cls(np.empty(0), {s: np.empty(0) for s in SLIPPAGES})
        return cls(np.concatenate([p.ts for p in parts]),
                   {s: np.concatenate([p.changes[s] for p in parts]) for s in SLIPPAGES})

    def _index(self, t: dt.datetime) -> int:
        return int(np.searchsorted(self.ts, t.timestamp(), "left"))

    def part(self, a: dt.datetime, b: dt.datetime) -> "Series":
        i, j = self._index(a), self._index(b)
        return Series(self.ts[i:j], {s: c[i:j] for s, c in self.changes.items()})

    def stats(self, a: dt.datetime, b: dt.datetime, share: float | None = None) -> dict:
        """`selection.window_stats`' figures over the trades closed in [a, b)."""
        i, j = self._index(a), self._index(b)
        out = {f"pnl{s}": float(self._cum[s][j] - self._cum[s][i]) for s in SLIPPAGES}
        out["trades"] = j - i
        c5 = self.changes["5"][i:j]
        if j > i:
            equity = np.cumsum(c5)
            out["dd5"] = float(np.max(np.maximum.accumulate(np.maximum(equity, 0.0)) - equity))
        else:
            out["dd5"] = 0.0
        out["top_share5"] = 0.0 if share is None else top_share(c5, share)
        return out

    def sums(self, edges: list[dt.datetime], s: str) -> np.ndarray:
        """The PnL of each [edges[k], edges[k+1])."""
        idx = np.searchsorted(self.ts, [e.timestamp() for e in edges], "left")
        return np.diff(self._cum[s][idx])

    def hourly(self) -> dict[int, dict]:
        """Hour start (epoch seconds) -> trades and PnL closed in it."""
        if not len(self.ts):
            return {}
        hours = (self.ts // 3600).astype(np.int64) * 3600
        keys, starts = np.unique(hours, return_index=True)
        counts = np.diff(np.append(starts, len(hours)))
        sums = {s: np.add.reduceat(self.changes[s], starts) for s in SLIPPAGES}
        return {int(k): {"trades": int(counts[n]), **{f"pnl{s}": float(sums[s][n]) for s in SLIPPAGES}}
                for n, k in enumerate(keys)}


def _totals(stats: dict) -> dict:
    return {k: stats[k] for k in ("pnl0", "pnl5", "pnl10", "dd5", "trades")}


# ------------------------------------------------------------------ choosing


def ranking(candidates: list[dict], a: dt.datetime, b: dt.datetime, grid: list[dict],
            settings: selection.SelectionSettings) -> list[dict]:
    """`selection.rank` of each candidate's `series` over [a, b)."""
    return selection.rank([(c["params"], c["series"].stats(a, b, settings.top_share)) for c in candidates],
                          grid, settings)


def choose(candidates: list[dict], a: dt.datetime, b: dt.datetime, grid: list[dict],
           settings: selection.SelectionSettings) -> dict | None:
    """The candidate `selection` would choose on [a, b), or None when none passes."""
    ranked = ranking(candidates, a, b, grid, settings)
    if not ranked or not ranked[0]["passes"]:
        return None
    return next(c for c in candidates if c["params"] == ranked[0]["params"])


def walk(runs: list[dict], grid: list[dict], settings: selection.SelectionSettings,
         first: dt.datetime, stop: dt.datetime, test_from: dt.datetime, rule: Rule, cache: dict | None = None) -> dict:
    """One rule over one group: the folds, and the out-of-sample totals over [test_from, stop).

    `cache` keeps each choice by its training span, so rules that share one --
    an expanding window chosen hourly and daily -- rank it once. `_series` is
    the out-of-sample curve and `_spans` which run traded when; neither is written.
    """
    cache = {} if cache is None else cache
    history = parse_span(rule.history)
    folds, parts, spans = [], [], []
    start = test_from
    while start < stop:
        end = stop if rule.every == NEVER else min(stop, advance(start, parse_span(rule.every)))
        train_from = first if rule.window == "expanding" else max(first, advance(start, history, back=True))
        if (train_from, start) not in cache:
            cache[train_from, start] = choose(runs, train_from, start, grid, settings)
        run = cache[train_from, start]
        if run is not None:
            parts.append(run["series"].part(start, end))
        tested = run["series"].stats(start, end) if run else {f"pnl{s}": 0.0 for s in SLIPPAGES}
        folds.append({"train": [stamp(train_from), stamp(start)], "test": [stamp(start), stamp(end)],
                      "params": run["params"] if run else None, "run_id": run["run_id"] if run else None,
                      **{f"pnl{s}": tested[f"pnl{s}"] for s in SLIPPAGES}})
        spans.append((start, end, run))
        start = end

    series = Series.concat(parts)
    ids = [f["run_id"] for f in folds]
    return {
        "rule": asdict(rule),
        "label": rule.label,
        "folds": folds,
        "out_of_sample": {
            **_totals(series.stats(test_from, stop)),
            "folds": len(folds),
            "profitable_folds": sum(1 for f in folds if f["params"] is not None and f["pnl5"] > 0),
            "sat_out": sum(1 for f in folds if f["params"] is None),
            "switches": sum(1 for a, b in zip(ids, ids[1:]) if a != b),
        },
        "_series": series,
        "_spans": spans,
    }


def public(report: dict) -> dict:
    """A report without the curves it was computed from."""
    return {k: v for k, v in report.items() if not k.startswith("_")}


def hindsight(runs: list[dict], grid: list[dict], settings: selection.SelectionSettings,
              first: dt.datetime, stop: dt.datetime, test_from: dt.datetime) -> dict:
    """The set chosen on the whole range, scored on the test hours it has already seen."""
    run = choose(runs, first, stop, grid, settings)
    series = run["series"] if run else Series.concat([])
    return {"label": "hindsight", "params": run["params"] if run else None,
            "run_id": run["run_id"] if run else None,
            "out_of_sample": {**_totals(series.stats(test_from, stop)), "folds": 1, "profitable_folds": None,
                              "sat_out": int(run is None), "switches": 0},
            "_series": series}


# ------------------------------------------------------------------ the groups


def _load(root: Path) -> list[dict]:
    runs = best.load_runs(root, None)
    for r in runs:
        r["series"] = Series.of(r["result"].pop("trades"))
    return runs


def _tasks(root: Path, settings: selection.SelectionSettings, rule_list: list[Rule],
           test_from: dt.datetime | None) -> list[tuple]:
    """`(strategy, start_str, end_str, group, members, first, stop, test_from)` for every range and group."""
    tasks = []
    for (strategy, start_str, end_str), groups in sorted(best.group(_load(root), settings.group_by).items(),
                                                         key=lambda kv: tuple(str(k) for k in kv[0])):
        dt_range = DtRange.from_strings(start_str, end_str)
        first, stop = complete_hours(dt_range.start_ts, dt_range.end_ts)
        tf = test_from or default_test_from(first, rule_list)
        if not first < tf < stop:
            LOG.warning(f"{strategy} {start_str}..{end_str}: test from {stamp(tf)} leaves no training "
                        f"or no test hours; skipped")
            continue
        for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
            tasks.append((strategy, start_str, end_str, dict(key), members, first, stop, tf))
    return tasks


def _map(one, tasks: list) -> list:
    # A group's runs are its own, so groups share nothing and run side by side.
    with ThreadPoolExecutor(max_workers=max(1, min(len(tasks), os.cpu_count() or 1)),
                            thread_name_prefix="walk-forward-") as executor:
        return list(executor.map(one, tasks))


def analyse(root: Path, rule_list: list[Rule], settings: selection.SelectionSettings,
            test_from: dt.datetime | None = None) -> list[dict]:
    """Every rule, and hindsight, for every range and group under `root`."""

    def one(task) -> dict:
        strategy, start_str, end_str, group, members, first, stop, tf = task
        grid, cache = best.axes(members), {}
        return {
            "strategy": strategy, "start_str": start_str, "end_str": end_str,
            "group": group, "runs": len(members),
            "test": [stamp(tf), stamp(stop)],
            "rules": [public(walk(members, grid, settings, first, stop, tf, r, cache)) for r in rule_list],
            "hindsight": public(hindsight(members, grid, settings, first, stop, tf)),
        }

    return _map(one, _tasks(root, settings, rule_list, test_from))


def _group_name(group: dict) -> str:
    return " ".join(str(v) for v in group.values()) or "all"


def sums(groups: list[dict], names: list[list[str]]) -> list[dict]:
    """Out-of-sample PnL of trading several groups side by side, e.g. two schedules that split the day.

    Each entry of `names` lists group names (`_group_name`, the values joined)
    whose totals are added, per range and rule. No drawdown: the groups'
    curves are not added trade by trade.
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
                    for s in SLIPPAGES}})
            rows.append({"label": "hindsight", **{
                f"pnl{s}": sum(m["hindsight"]["out_of_sample"][f"pnl{s}"] for m in members)
                for s in SLIPPAGES}})
            out.append({"strategy": strategy, "start_str": start_str, "end_str": end_str,
                        "name": " + ".join(parts), "rows": rows})
    return out


def _k(v) -> str:
    return "n/a" if v is None else f"{v / 1000:+.1f}k"


def table(groups: list[dict], summed: list[dict]) -> list[str]:
    lines = []
    head = f"  {'rule':28s} {'pnl5':>9s} {'pnl10':>9s} {'dd5':>8s} {'profitable':>11s} {'switches':>8s} {'sat out':>7s}"
    for g in groups:
        lines.append(f"{g['strategy']} {g['start_str']}..{g['end_str']} {_group_name(g['group'])} "
                     f"({g['runs']} runs), tested {g['test'][0]}..{g['test'][1]}")
        lines.append(head)
        for r in [*g["rules"], g["hindsight"]]:
            o = r["out_of_sample"]
            profitable = "" if o["profitable_folds"] is None else f"{o['profitable_folds']}/{o['folds']}"
            lines.append(f"  {r['label']:28s} {_k(o['pnl5']):>9s} {_k(o['pnl10']):>9s} {o['dd5'] / 1000:7.1f}k "
                         f"{profitable:>11s} {o['switches']:8d} {o['sat_out']:7d}")
    for s in summed:
        lines.append(f"{s['strategy']} {s['start_str']}..{s['end_str']} {s['name']}")
        lines.append(f"  {'rule':28s} {'pnl5':>9s} {'pnl10':>9s}")
        for r in s["rows"]:
            lines.append(f"  {r['label']:28s} {_k(r['pnl5']):>9s} {_k(r['pnl10']):>9s}")
    return lines


def _new_path(out_dir: Path, name: str, suffix: str = "") -> Path:
    n = int(time.time())
    while (out_dir / f"{name}_{n}{suffix}").exists():
        n += 1
    return out_dir / f"{name}_{n}{suffix}"


def run(root: Path, rule_list: list[Rule], settings: selection.SelectionSettings,
        test_from: dt.datetime | None = None, sum_names: list[list[str]] | None = None) -> Path | None:
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
    path = _new_path(out_dir, "walk_forward", ".json")
    path.write_text(json.dumps({"settings": asdict(settings), "rules": [asdict(r) for r in rule_list],
                                "groups": groups, "sums": summed}, indent=2))
    path.with_suffix(".txt").write_text("\n".join(lines) + "\n")
    LOG.info(f"Saved {path}")
    return path


# ------------------------------------------------------------------ training


def baseline_run(members: list[dict], defaults: dict, group: dict) -> dict | None:
    """The run of the grid's default values -- with the group's own values for the grouped parameters."""
    want = {**defaults, **group}
    return next((r for r in members if all(r["params"].get(k) == v for k, v in want.items())), None)


def day_edges(start: dt.datetime, stop: dt.datetime) -> list[dt.datetime]:
    """`start`, every JST midnight after it and before `stop`, and `stop`."""
    edges = [start]
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
    while midnight < stop:
        edges.append(midnight)
        midnight += dt.timedelta(days=1)
    return edges + [stop]


def block_bootstrap(diffs: np.ndarray, block: int, resamples: int, rng: np.random.Generator) -> float | None:
    """One-sided p-value of a mean daily difference above zero, by a circular moving-block bootstrap.

    The differences are centred, so the resamples are drawn under "no
    difference" with the days' own autocorrelation kept within each block.
    """
    n = len(diffs)
    if n < 2:
        return None
    block = min(block, n)
    observed = diffs.mean()
    centred = diffs - observed
    blocks = math.ceil(n / block)
    starts = rng.integers(0, n, size=(resamples, blocks))
    idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(resamples, -1)[:, :n]
    means = centred[idx].mean(axis=1)
    return float((np.sum(means >= observed) + 1) / (resamples + 1))


def sign_test(won: int, lost: int) -> float | None:
    """One-sided: the chance of winning at least `won` of `won + lost` coin flips."""
    n = won + lost
    if n == 0:
        return None
    return sum(math.comb(n, k) for k in range(won, n + 1)) / 2 ** n


def compare(trained: Series, base: Series, start: dt.datetime, stop: dt.datetime, folds: list[dict],
            block_days: int, resamples: int) -> dict:
    """Whether the trained curve beat the baseline over [start, stop), by day and by training fold."""
    edges = day_edges(start, stop)
    rng = np.random.default_rng(0)  # the same answer for the same folders
    out = {"days": len(edges) - 1, "block_days": block_days, "resamples": resamples}
    for s in ("5", "10"):
        d = trained.sums(edges, s) - base.sums(edges, s)
        sd = float(d.std(ddof=1)) if len(d) > 1 else 0.0
        out[f"pnl{s}"] = {
            "total_diff": float(d.sum()),
            "mean_daily_diff": float(d.mean()) if len(d) else 0.0,
            "t_stat": float(d.mean() / (sd / math.sqrt(len(d)))) if sd > 0 else None,
            "p_bootstrap": block_bootstrap(d, block_days, resamples, rng),
        }
    won = sum(1 for f in folds if f["test_pnl5"] > f["baseline_pnl5"])
    lost = sum(1 for f in folds if f["test_pnl5"] < f["baseline_pnl5"])
    out.update(folds_won=won, folds_lost=lost, p_sign=sign_test(won, lost))
    return out


def _rank_row(row: dict, by_params: dict) -> dict:
    c = by_params[selection._key(row["params"])]
    return {"key": c["key"], "label": c["label"], "passes": row["passes"], "score": row["score"],
            **{k: row[k] for k in ("pnl0", "pnl5", "pnl10", "dd5", "top_share5", "trades")}}


def _clip(spans: list[tuple], a: dt.datetime, b: dt.datetime, key: str) -> list[dict]:
    """Which run a candidate traded within [a, b)."""
    out = []
    for s, e, run in spans:
        s, e = max(s, a), min(e, b)
        if s < e:
            out.append({"from": stamp(s), "to": stamp(e), "rule": key,
                        "run_id": run["run_id"] if run else None, "params": run["params"] if run else None})
    return out


def _merge(switches: list[dict]) -> list[dict]:
    out = []
    for s in switches:
        if out and out[-1]["to"] == s["from"] and (out[-1]["rule"], out[-1]["run_id"]) == (s["rule"], s["run_id"]):
            out[-1]["to"] = s["to"]
        else:
            out.append(dict(s))
    return out


def train(members: list[dict], group: dict, settings: selection.SelectionSettings, wf: WalkForwardSettings,
          rule_list: list[Rule], defaults: dict, first: dt.datetime, stop: dt.datetime,
          inner_from: dt.datetime) -> dict | None:
    """Choose a rule every `retrain` on the `train_history` before, against the baseline, over one group.

    Returns the training as `write_training` writes it, or None when the range
    leaves no training fold.
    """
    grid, cache = best.axes(members), {}
    walks = [walk(members, grid, settings, first, stop, inner_from, r, cache) for r in rule_list]
    candidates = [{"key": r.key, "label": r.label, "params": r.params, "rule": asdict(r),
                   "series": w["_series"], "spans": w["_spans"]} for r, w in zip(rule_list, walks)]

    run = baseline_run(members, defaults, group) if wf.baseline == "default" else None
    if run is not None:
        base = {"kind": "default", "run_id": run["run_id"], "params": run["params"],
                "series": run["series"], "spans": [(first, stop, run)]}
    else:
        if wf.baseline == "default":
            LOG.warning(f"{_group_name(group)}: no run of the grid's default values; the baseline is "
                        f"the set chosen once at the start of the test")
        longest = max((r.history for r in rule_list), key=lambda h: advance(first, parse_span(h)))
        once = walk(members, grid, settings, first, stop, inner_from, Rule("expanding", longest, NEVER), cache)
        chosen = once["folds"][0]
        base = {"kind": "once", "run_id": chosen["run_id"], "params": chosen["params"],
                "series": once["_series"], "spans": once["_spans"]}
    baseline = {"key": "baseline", "label": f"no switching ({base['kind']})", "params": {"window": "baseline"},
                "series": base["series"], "spans": base["spans"]}

    everyone = candidates + [baseline]
    by_params = {selection._key(c["params"]): c for c in everyone}
    # A rule's history and interval are its numeric axes; "never" and the baseline have neither.
    outer_grid = [{"name": n, "values": [c["params"][n] for c in candidates if c["params"][n] is not None]}
                  for n in ("history_h", "every_h")]

    train_history, retrain = parse_span(wf.train_history), parse_span(wf.retrain)

    def window_before(t: dt.datetime) -> dt.datetime:
        return inner_from if wf.train_window == "expanding" else max(inner_from, advance(t, train_history, back=True))

    def pick(a: dt.datetime, b: dt.datetime) -> tuple[dict, list[dict]]:
        ranked = ranking(everyone, a, b, outer_grid, settings)
        top = by_params[selection._key(ranked[0]["params"])] if ranked and ranked[0]["passes"] else None
        return top, [_rank_row(r, by_params) for r in ranked]

    outer_from = advance(inner_from, train_history)
    if not outer_from < stop:
        LOG.warning(f"{_group_name(group)}: rules are tested from {stamp(inner_from)}, and {wf.train_history} "
                    f"of them to train on leaves no test before {stamp(stop)}; not trained")
        return None

    folds, parts, switches = [], [], []
    t = outer_from
    while t < stop:
        end = min(stop, advance(t, retrain))
        a = window_before(t)
        top, ranked = pick(a, t)
        chosen = top or baseline
        tested, base_tested = chosen["series"].stats(t, end), baseline["series"].stats(t, end)
        folds.append({"train": [stamp(a), stamp(t)], "test": [stamp(t), stamp(end)],
                      "chosen": chosen["key"], "label": chosen["label"], "rule": chosen.get("rule"),
                      "fallback": top is None,
                      **{f"test_pnl{s}": tested[f"pnl{s}"] for s in SLIPPAGES},
                      **{f"baseline_pnl{s}": base_tested[f"pnl{s}"] for s in SLIPPAGES},
                      "ranking": ranked})
        parts.append(chosen["series"].part(t, end))
        switches.extend(_clip(chosen["spans"], t, end, chosen["key"]))
        t = end

    trained = Series.concat(parts)
    switches = _merge(switches)
    top, _ = pick(window_before(stop), stop)
    current = top or baseline
    seen = hindsight(members, grid, settings, first, stop, outer_from)
    keys = [f["chosen"] for f in folds]
    ids = [s["run_id"] for s in switches]

    summary = {
        "trained": {**_totals(trained.stats(outer_from, stop)), "folds": len(folds),
                    "fallback_folds": sum(1 for f in folds if f["fallback"]),
                    "rule_changes": sum(1 for a, b in zip(keys, keys[1:]) if a != b),
                    "set_changes": sum(1 for a, b in zip(ids, ids[1:]) if a != b)},
        "baseline": {**_totals(baseline["series"].stats(outer_from, stop)), "kind": base["kind"],
                     "run_id": base["run_id"]},
        "hindsight": {**_totals(seen["_series"].stats(outer_from, stop)), "run_id": seen["run_id"]},
        "rules": {c["key"]: _totals(c["series"].stats(outer_from, stop)) for c in candidates},
        "significance": compare(trained, baseline["series"], outer_from, stop, folds, wf.block_days, wf.resamples),
        "current": {"chosen": current["key"], "label": current["label"], "rule": current.get("rule"),
                    "fallback": top is None, "train": [stamp(window_before(stop)), stamp(stop)]},
    }

    series = {"trained": trained, "baseline": baseline["series"], "hindsight": seen["_series"],
              **{c["key"]: c["series"] for c in candidates}}
    curve = []
    for name, s in series.items():
        for hour, row in s.part(outer_from, stop).hourly().items():
            curve.append({"ts": hour, "series": name, **row})
    curve.sort(key=lambda r: (r["ts"], r["series"]))

    return {
        "meta": {
            "inner_from": stamp(inner_from),
            "test": [stamp(outer_from), stamp(stop)],
            "baseline": {"kind": base["kind"], "run_id": base["run_id"], "params": base["params"]},
            "rules": [{"key": c["key"], "label": c["label"], **c["rule"], **c["params"]} for c in candidates],
        },
        "summary": summary,
        "folds": folds,
        "switches": switches,
        "curve": curve,
    }


TRAIN_FILES = ("meta.json", "folds.json", "switches.json", "curve.csv", "summary.json")
CURVE_COLUMNS = ["ts", "series", "trades", *(f"pnl{s}" for s in SLIPPAGES)]


def write_training(root: Path, train_id: str, training: dict) -> Path:
    """`post_analysis/train/<train_id>/`, built beside its final name and renamed into place.

        meta.json      what was trained: env, range, group, rules, baseline, settings
        folds.json     each retraining: its windows, every candidate's ranking, the choice and its test
        switches.json  which rule and parameter set the trained curve traded, and when
        curve.csv      PnL closed per JST hour, per series: trained, baseline, hindsight, each rule
        summary.json   the totals, the significance and the rule to trade now -- written last
    """
    final = Path(root) / OUTPUT_DIR / TRAIN_DIR / train_id
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"{train_id}.tmp-{os.getpid()}")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    for name in ("meta", "folds", "switches"):
        (tmp / f"{name}.json").write_text(json.dumps(training[name], indent=2))
    with open(tmp / "curve.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, CURVE_COLUMNS)
        writer.writeheader()
        writer.writerows(training["curve"])
    (tmp / "summary.json").write_text(json.dumps(training["summary"], indent=2))
    tmp.rename(final)
    return final


def train_table(meta: dict, summary: dict) -> list[str]:
    sig = summary["significance"]
    lines = [f"{meta['strategy']} {meta['start_str']}..{meta['end_str']} {_group_name(meta['group'])} "
             f"({meta['runs']} runs), trained every {meta['settings']['walk_forward']['retrain']} "
             f"on {meta['settings']['walk_forward']['train_history']}, tested {meta['test'][0]}..{meta['test'][1]}",
             f"  {'':28s} {'pnl5':>9s} {'pnl10':>9s} {'dd5':>8s}"]
    rows = [("trained", summary["trained"]), (f"baseline ({summary['baseline']['kind']})", summary["baseline"]),
            ("hindsight", summary["hindsight"])]
    labels = {r["key"]: r["label"] for r in meta["rules"]}
    rows += [(labels[k], v) for k, v in summary["rules"].items()]
    for name, o in rows:
        lines.append(f"  {name:28s} {_k(o['pnl5']):>9s} {_k(o['pnl10']):>9s} {o['dd5'] / 1000:7.1f}k")
    p5, p10 = sig["pnl5"], sig["pnl10"]
    lines.append(f"  trained - baseline over {sig['days']} days: pnl5 {_k(p5['total_diff'])} "
                 f"(p={_p(p5['p_bootstrap'])}), pnl10 {_k(p10['total_diff'])} (p={_p(p10['p_bootstrap'])}); "
                 f"folds won {sig['folds_won']}/{sig['folds_won'] + sig['folds_lost']} (p={_p(sig['p_sign'])})")
    lines.append(f"  now: {summary['current']['label']}{' (nothing passed)' if summary['current']['fallback'] else ''}")
    return lines


def _p(p) -> str:
    return "n/a" if p is None else f"{p:.3f}"


def run_train(root: Path, wf: WalkForwardSettings, settings: selection.SelectionSettings,
              defaults: dict) -> list[Path]:
    """Train every range and group under `root`, log each, and write each to `post_analysis/train/`."""
    rule_list = wf.rules()
    test_from = parse_time(wf.test_from) if wf.test_from else None
    created = time.time()

    def one(task):
        strategy, start_str, end_str, group, members, first, stop, inner_from = task
        training = train(members, group, settings, wf, rule_list, defaults, first, stop, inner_from)
        if training is not None:
            training["meta"] = {"env": Path(root).name, "strategy": strategy, "start_str": start_str,
                                "end_str": end_str, "group": group, "runs": len(members),
                                "created_ts": created, **training["meta"],
                                "settings": {"selection": asdict(settings), "walk_forward": asdict(wf)}}
        return training

    trainings = [t for t in _map(one, _tasks(root, settings, rule_list, test_from)) if t is not None]
    out_dir = Path(root) / OUTPUT_DIR / TRAIN_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _new_path(out_dir, "train").name
    paths = []
    for n, training in enumerate(trainings):
        for line in train_table(training["meta"], training["summary"]):
            LOG.info(line)
        training["meta"]["train_id"] = f"{base}_{n}"
        paths.append(write_training(root, f"{base}_{n}", training))
        LOG.info(f"Saved {paths[-1]}")
    return paths
