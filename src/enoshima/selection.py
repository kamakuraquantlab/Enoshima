"""Choosing a parameter set from a sweep: filtered, scored, smoothed, and tested out of sample.

Ranking every result on one number over the whole range picks the set that fit
that range best, which is not the same as the set most likely to keep working.
Three things here push back against that, all computed from the run folders
a sweep has already written -- nothing is replayed again.

## Filters

A set must not lose money at the pessimistic slippage (`positive_pnl10`), and
may be required to trade enough (`min_trades`) and to stay within a drawdown
(`max_drawdown`, on pnl5, in the quote currency). A set that fails is ranked
below every set that passes, whatever its PnL.

## Score

Four figures, each asking something the others do not:

| Figure | Better | Asks |
|---|---|---|
| `pnl5` | higher | what it earns at the realistic slippage |
| `pnl10` | higher | whether it survives degraded execution |
| `dd5` | lower | how deep it falls in a period that does not suit it |
| `top_share5` | lower | how much of its winnings its best `top_share` of trades made |

The last is the distribution test: a set whose profit is a handful of big wins
depends on the next range holding the same handful. It is the pnl5 of the best
`ceil(top_share * trades)` trades over the pnl5 of every winning trade, so it
does not grow with the number of trades or the size of a position. Net pnl5 is
not the denominator, and "pnl5 without the best trades" is not the figure: a
trend follower lives on large wins and loses on most trades, so both go
negative for every set and end up ranking how *few* trades a set made.

Each figure is turned into a percentile among the sets that pass the filters --
1 for the best, 0 for the worst -- and the score is their mean. Ranks rather
than a weighted sum, because the four are in different units and a weight would
be a fifth parameter to fit. A set is then chosen for being good on all four,
not exceptional on one: the pnl5 leader with twice the drawdown of the runner-up
gives back most of its lead.

`score: "pnl5"` ranks on pnl5 alone, which was the rule before; it stays for
comparison.

## Neighbours

Each figure is averaged with its neighbours on the grid before it is ranked:
the sets that differ in one numeric parameter by one step. A peak that its
neighbours do not share is likely a fit to noise; a plateau is not. Categorical
parameters (`"nofilter"`, `"us_off"`) have no order, so they have no neighbours.

## Walk-forward

With each run folder's `by_month.json` (see `ledger`), the sweep is replayed
in time without being replayed in fact: choose the best set on the months
before a test, score that choice on the `test_months` that follow, step
forward by `test_months`, repeat. The sum of the test months is what the
selection procedure would have earned had it been run month by month; it is
the number to believe, more than any in-sample best.

`window` says which earlier months a choice is made on. `"expanding"` takes
every complete month from the start of the range, so `train_months` is only the
history the first test needs, and the set recommended now is chosen on the
whole range. `"rolling"` takes the latest `train_months`, and recommends on the
most recent ones. Expanding is the default: on three trend-following sweeps a rolling six
months chose on noise -- four percentiles of 45 sets over six months are mostly
that -- and turned a set's good year into a bad half-year's verdict.

Only complete JST months inside the range are used, so a range ending on the
first of a month does not make that one day a test.
"""

import datetime as dt
import math
from dataclasses import asdict, dataclass

from .ledger import JST

SLIPPAGES = ("0", "5", "10")
SCORES = ("balanced", "pnl5")
WINDOWS = ("expanding", "rolling")

# figure -> whether higher is better
CRITERIA = {"pnl5": True, "pnl10": True, "dd5": False, "top_share5": False}


@dataclass
class SelectionSettings:
    """`enoshima.json`'s `"selection"`. Every key optional."""

    positive_pnl10: bool = True
    min_trades: int = 0
    max_drawdown: float | None = None
    neighbors: bool = True
    window: str = "expanding"
    train_months: int = 6
    test_months: int = 1
    score: str = "balanced"
    top_share: float = 0.05

    def __post_init__(self):
        if self.window not in WINDOWS:
            raise ValueError(f"selection window {self.window!r} is not one of {WINDOWS}")
        if self.score not in SCORES:
            raise ValueError(f"selection score {self.score!r} is not one of {SCORES}")
        if not 0 < self.top_share < 1:
            raise ValueError("selection top_share is a share of trades, in (0, 1)")
        if self.train_months < 0 or self.test_months < 1:
            raise ValueError("selection needs train_months >= 0 (0 turns walk-forward off) and test_months >= 1")


# ----------------------------------------------------------------- windows


def top_share(changes: list[float], share: float) -> float:
    """What the best `ceil(share * len)` of `changes` add up to, over the sum of the positive ones.

    1 when there are no winning trades: nothing could be less evenly spread.
    """
    gross = sum(c for c in changes if c > 0)
    if gross <= 0:
        return 1.0
    k = math.ceil(share * len(changes))
    return sum(c for c in sorted(changes, reverse=True)[:k] if c > 0) / gross


def window_stats(periods: dict, months: list[str], trade_pnl5: dict | None = None, share: float = 0.05) -> dict:
    """PnL, trades and pnl5's maximum drawdown over a run of months.

    A month a set did not trade in is absent from its periods and counts as
    zero. The drawdown is exact: each month records its high, low and worst
    drawdown from its own start, and the running high is carried across.
    `trade_pnl5`, each month's trades' pnl5 changes, gives `top_share5`;
    without it that is 0, the same for every set.
    """
    stats = {f"pnl{s}": 0.0 for s in SLIPPAGES}
    stats.update(trades=0, dd5=0.0)
    high = 0.0
    for month in months:
        m = periods.get(month)
        if m is None:
            continue
        start = stats["pnl5"]
        stats["dd5"] = max(stats["dd5"], m["dd5"], high - (start + m["trough5"]))
        high = max(high, start + m["peak5"])
        for s in SLIPPAGES:
            stats[f"pnl{s}"] += m[f"pnl{s}"]
        stats["trades"] += m["trades"]
    stats["top_share5"] = (0.0 if trade_pnl5 is None
                           else top_share([c for m in months for c in trade_pnl5.get(m, ())], share))
    return stats


def full_range_stats(result: dict, trade_pnl5: dict | None = None, share: float = 0.05) -> dict:
    """The same figures from a run's whole-range summary."""
    stats = {f"pnl{s}": result.get(f"pnl{s}", 0.0) for s in SLIPPAGES}
    stats.update(trades=result.get("pnl_cnt5", 0), dd5=result.get("max_drawdown5", 0.0))
    stats["top_share5"] = (0.0 if trade_pnl5 is None
                           else top_share([c for cs in trade_pnl5.values() for c in cs], share))
    return stats


def complete_months(start_ts: float, end_ts: float) -> list[str]:
    """JST months lying wholly inside [start_ts, end_ts], as `YYYY-MM`."""
    start = dt.datetime.fromtimestamp(start_ts, JST)
    end = dt.datetime.fromtimestamp(end_ts, JST)
    months = []
    first = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first < start:
        first = _next_month(first)
    month = first
    while _next_month(month) - dt.timedelta(microseconds=1) <= end:
        months.append(month.strftime("%Y-%m"))
        month = _next_month(month)
    return months


def _next_month(d: dt.datetime) -> dt.datetime:
    return d.replace(year=d.year + d.month // 12, month=d.month % 12 + 1)


# ----------------------------------------------------------------- ranking


def passes(stats: dict, settings: SelectionSettings) -> bool:
    if settings.positive_pnl10 and stats["pnl10"] <= 0:
        return False
    if stats["trades"] < settings.min_trades:
        return False
    if settings.max_drawdown is not None and stats["dd5"] > settings.max_drawdown:
        return False
    return True


def _key(params: dict) -> tuple:
    return tuple(sorted(params.items()))


def neighbours(params: dict, grid: list[dict] | None) -> list[tuple]:
    """Keys of the sets one step away along each numeric axis of the grid."""
    out = []
    for axis in grid or []:
        name, values = axis["name"], axis["values"]
        if name not in params or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            continue
        ordered = sorted(set(values))
        if params[name] not in ordered:
            continue
        i = ordered.index(params[name])
        for j in (i - 1, i + 1):
            if 0 <= j < len(ordered):
                out.append(_key({**params, name: ordered[j]}))
    return out


def percentiles(values: list[float], higher_is_better: bool) -> list[float]:
    """Each value's place among `values`: 1 for the best, 0 for the worst, ties averaged."""
    n = len(values)
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i], reverse=not higher_is_better)  # worst first
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 / (n - 1)
        i = j + 1
    return out


def rank(candidates: list[tuple[dict, dict]], grid: list[dict] | None, settings: SelectionSettings) -> list[dict]:
    """`(params, stats)` pairs, best first: passing sets before failing, then by score."""
    figures = ("pnl5",) if settings.score == "pnl5" else tuple(CRITERIA)
    raw = {_key(params): stats for params, stats in candidates}
    rows = []
    for params, stats in candidates:
        around = [raw[k] for k in neighbours(params, grid) if k in raw] if settings.neighbors else []
        smoothed = {f: sum(s.get(f, 0.0) for s in [stats, *around]) / (1 + len(around)) for f in figures}
        rows.append({"params": params, "passes": passes(stats, settings), "score": 0.0,
                     "smoothed": smoothed, **stats})

    if settings.score == "pnl5":
        for r in rows:
            r["score"] = r["smoothed"]["pnl5"]
    else:
        # Ranked among the sets that pass, so a set that fails cannot move them;
        # one that fails is scored among the failures, and ranks below anyway.
        for group in ([r for r in rows if r["passes"]], [r for r in rows if not r["passes"]]):
            if not group:
                continue
            places = {f: percentiles([r["smoothed"][f] for r in group], CRITERIA[f]) for f in figures}
            for i, r in enumerate(group):
                r["places"] = {f: places[f][i] for f in figures}
                r["score"] = sum(r["places"].values()) / len(figures)
    rows.sort(key=lambda r: (not r["passes"], -r["score"]))
    return rows


# ------------------------------------------------------------ walk-forward


def _window(run: dict, months: list[str], settings: SelectionSettings) -> dict:
    return window_stats(run["result"]["periods"], months, run["result"].get("trade_pnl5"), settings.top_share)


def walk_forward(runs: list[dict], months: list[str], grid: list[dict] | None,
                 settings: SelectionSettings) -> dict:
    """Choose on the months before, test on the next `test_months`, step, repeat.

    `runs` are run logs whose results carry `periods`, a run folder's
    `by_month.json`, and optionally `trade_pnl5`, its trades' pnl5 changes by
    month. Returns the folds, the
    out-of-sample totals, and the set chosen on the most recent window.
    """
    train, test = settings.train_months, settings.test_months
    folds = []
    for k in range(train, len(months) - test + 1, test):
        train_months = months[:k] if settings.window == "expanding" else months[k - train:k]
        test_months = months[k:k + test]
        ranked = rank([(r["params"], _window(r, train_months, settings)) for r in runs], grid, settings)
        fold = {"train": [train_months[0], train_months[-1]], "test": [test_months[0], test_months[-1]]}
        chosen = ranked[0] if ranked and ranked[0]["passes"] else None
        if chosen is None:
            fold.update(params=None, **{f"pnl{s}": 0.0 for s in SLIPPAGES})
        else:
            run = next(r for r in runs if r["params"] == chosen["params"])
            tested = window_stats(run["result"]["periods"], test_months)
            fold.update(params=chosen["params"], **{f"pnl{s}": tested[f"pnl{s}"] for s in SLIPPAGES})
        folds.append(fold)

    latest_months = months if settings.window == "expanding" else months[-train:]
    latest = rank([(r["params"], _window(r, latest_months, settings)) for r in runs], grid, settings)
    return {
        "folds": folds,
        "out_of_sample": {
            **{f"pnl{s}": sum(f[f"pnl{s}"] for f in folds) for s in SLIPPAGES},
            "months": sum(1 for f in folds if f["params"] is not None) * test,
            "profitable_folds": sum(1 for f in folds if f["params"] is not None and f["pnl5"] > 0),
            "folds": len(folds),
        },
        "latest": latest,
    }


def select(runs: list[dict], start_ts: float, end_ts: float, grid: list[dict] | None,
           settings: SelectionSettings) -> dict:
    """Everything `--optimize` reports: the method, the recommendation, and why.

    Walk-forward when the range holds enough complete months; otherwise the
    whole-range ranking, filtered and smoothed.
    """
    months = complete_months(start_ts, end_ts)
    enough = settings.train_months > 0 and len(months) >= settings.train_months + settings.test_months
    out = {"settings": asdict(settings)}

    if enough:
        wf = walk_forward(runs, months, grid, settings)
        out.update(method="walk-forward", walk_forward={k: wf[k] for k in ("folds", "out_of_sample")}, ranking=wf["latest"])
    else:
        ranked = rank([(r["params"], full_range_stats(r["result"], r["result"].get("trade_pnl5"), settings.top_share))
                       for r in runs], grid, settings)
        reason = ("walk-forward off" if settings.train_months == 0
                  else f"{len(months)} complete month(s), fewer than train_months + test_months")
        out.update(method="whole range", reason=reason, ranking=ranked)

    top = out["ranking"][0] if out["ranking"] else None
    out["recommended"] = top["params"] if top and top["passes"] else None
    return out
