"""Choosing a parameter set from a sweep: filtered, smoothed, and tested out of sample.

Ranking every result on one number over the whole range picks the set that fit
that range best, which is not the same as the set most likely to keep working.
Three things here push back against that, all computed from the run logs a
sweep has already written -- nothing is replayed again.

## Filters

A set must not lose money at the pessimistic slippage (`positive_pnl10`), and
may be required to trade enough (`min_trades`) and to stay within a drawdown
(`max_drawdown`, on pnl5, in the quote currency). A set that fails is ranked
below every set that passes, whatever its PnL.

## Neighbours

A set's score is pnl5 averaged with its neighbours on the grid: the sets that
differ from it in one numeric parameter by one step. A peak that its neighbours
do not share is likely a fit to noise; a plateau is not. Categorical parameters
(`"nofilter"`, `"us_off"`) have no order, so they have no neighbours.

## Walk-forward

With `result.periods` in the run logs (see `result`), the sweep is replayed in
time without being replayed in fact: choose the best set on `train_months`
complete months, score that choice on the `test_months` after them, step
forward by `test_months`, repeat. The sum of the test months is what the
selection procedure would have earned had it been run month by month; it is
the number to believe, more than any in-sample best. The set recommended now is
the one chosen on the most recent `train_months`.

Only complete JST months inside the range are used, so a range ending on the
first of a month does not make that one day a test.
"""

import datetime as dt
from dataclasses import asdict, dataclass

from .result import JST

SLIPPAGES = ("0", "5", "10")


@dataclass
class SelectionSettings:
    """`enoshima.json`'s `"selection"`. Every key optional."""

    positive_pnl10: bool = True
    min_trades: int = 0
    max_drawdown: float | None = None
    neighbors: bool = True
    train_months: int = 6
    test_months: int = 1

    def __post_init__(self):
        if self.train_months < 0 or self.test_months < 1:
            raise ValueError("selection needs train_months >= 0 (0 turns walk-forward off) and test_months >= 1")


# ----------------------------------------------------------------- windows


def window_stats(periods: dict, months: list[str]) -> dict:
    """PnL, trades and pnl5's maximum drawdown over a run of months.

    A month a set did not trade in is absent from its periods and counts as
    zero. The drawdown is exact: each month records its high, low and worst
    drawdown from its own start, and the running high is carried across.
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
    return stats


def full_range_stats(result: dict) -> dict:
    """The same figures from a whole-range result, for run logs without periods."""
    stats = {f"pnl{s}": result.get(f"pnl{s}", 0.0) for s in SLIPPAGES}
    stats.update(trades=result.get("pnl_cnt5", 0), dd5=result.get("max_drawdown5", 0.0))
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


def rank(candidates: list[tuple[dict, dict]], grid: list[dict] | None, settings: SelectionSettings) -> list[dict]:
    """`(params, stats)` pairs, best first: passing sets before failing, then by score."""
    raw = {_key(params): stats["pnl5"] for params, stats in candidates}
    rows = []
    for params, stats in candidates:
        score = stats["pnl5"]
        if settings.neighbors:
            around = [raw[k] for k in neighbours(params, grid) if k in raw]
            score = (score + sum(around)) / (1 + len(around))
        rows.append({"params": params, "passes": passes(stats, settings), "score": score, **stats})
    rows.sort(key=lambda r: (not r["passes"], -r["score"]))
    return rows


# ------------------------------------------------------------ walk-forward


def walk_forward(runs: list[dict], months: list[str], grid: list[dict] | None,
                 settings: SelectionSettings) -> dict:
    """Choose on `train_months`, test on the next `test_months`, step, repeat.

    `runs` are run logs whose results carry `periods`. Returns the folds, the
    out-of-sample totals, and the set chosen on the most recent window.
    """
    train, test = settings.train_months, settings.test_months
    folds = []
    for k in range(train, len(months) - test + 1, test):
        train_months, test_months = months[k - train:k], months[k:k + test]
        ranked = rank([(r["params"], window_stats(r["result"]["periods"], train_months)) for r in runs], grid, settings)
        fold = {"train": [train_months[0], train_months[-1]], "test": [test_months[0], test_months[-1]]}
        chosen = ranked[0] if ranked and ranked[0]["passes"] else None
        if chosen is None:
            fold.update(params=None, **{f"pnl{s}": 0.0 for s in SLIPPAGES})
        else:
            run = next(r for r in runs if r["params"] == chosen["params"])
            tested = window_stats(run["result"]["periods"], test_months)
            fold.update(params=chosen["params"], **{f"pnl{s}": tested[f"pnl{s}"] for s in SLIPPAGES})
        folds.append(fold)

    latest = rank([(r["params"], window_stats(r["result"]["periods"], months[-train:])) for r in runs], grid, settings)
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

    Walk-forward when every run log has periods and the range holds enough
    complete months; otherwise the whole-range ranking, filtered and smoothed.
    """
    months = complete_months(start_ts, end_ts)
    have_periods = all("periods" in r.get("result", {}) for r in runs)
    enough = settings.train_months > 0 and len(months) >= settings.train_months + settings.test_months
    out = {"settings": asdict(settings)}

    if have_periods and enough:
        wf = walk_forward(runs, months, grid, settings)
        out.update(method="walk-forward", walk_forward={k: wf[k] for k in ("folds", "out_of_sample")}, ranking=wf["latest"])
    else:
        ranked = rank([(r["params"], full_range_stats(r["result"])) for r in runs], grid, settings)
        reason = ("run logs without periods; re-run with --force to record them" if not have_periods
                  else "walk-forward off" if settings.train_months == 0
                  else f"{len(months)} complete month(s), fewer than train_months + test_months")
        out.update(method="whole range", reason=reason, ranking=ranked)

    top = out["ranking"][0] if out["ranking"] else None
    out["recommended"] = top["params"] if top and top["passes"] else None
    return out
