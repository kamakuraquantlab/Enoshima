"""Strict weekly mode: a backtest computed a week at a time, so a longer one is built from weeks.

`enoshima run --weekly` widens the requested range to the ISO weeks that cover
it, Monday 00:00 to Sunday 24:00 JST, and replays each week on its own: a fresh
strategy and simulated exchange, fed `preload_hours` before the Monday and
reporting from the Monday on. A week's result is then a function of the env,
the strategy, the parameters, the preload and the week -- nothing else -- so

- a week computed for one window is the week any later window needs, and is
  skipped rather than replayed: a year extended by a month replays four weeks;
- a window's result is its weeks' trades, concatenated, and `summarize`d.

The week is the ISO week because `ledger`'s `by_week.json` already is, and
JST because bronze's day partitions are: a week is exactly seven files.

## What it does not promise

Equality with one continuous replay of the same window. Each week starts cold,
so a position opened before the preload and still open on the Monday is in
neither week, and a strategy whose state needs more history than the preload
diverges from the continuous run. `open_at_end` in a week's summary counts the
positions still open when its replay ended: the ones that its own figures do
not contain, and the next week has only if its preload re-creates them.

## The run folder is cumulative

One folder per environment, strategy revision, parameter set and preload --
no dates in its id -- and every weekly run of them adds to it:

    run_log/<run_id>/meta.json, summary.json,   rebuilt from every week on disk (`rebuild`):
                     trades.csv, by_*.json      an ordinary run over the weeks' range
    run_log/<run_id>/weeks/<YYYY-Www>/          what a run writes, a week at a time:
                     meta.json                  the week's provenance: batch, bucket, timings
                     summary.json               `summarize`'s figures, plus the order counters
                     trades.csv

## As much as there is data for

The weekly run is the main one, and is asked for ranges the data does not yet
reach -- to today, say. Each week is clipped to the complete days on disk,
the first to the last day every stream the strategy reads has, so the first
and last weeks may be partial; a week with none of those days is not run. A
partial week is recorded with its own `start_str`/`end_str` and `partial`,
and is run again by any later weekly run whose data reaches further into it:
a week grows to full without `--force`. A day missing *between* the first and
last is not clipped; it replays as empty, with the usual warning.

The root is never the current run's result. It is the weeks', so its range is
the first week's Monday to the last week's Sunday, and `select`,
`walk-forward` and `results` read it like any other run folder. For that range
to be true the weeks must be contiguous: a run fills every gap between the
weeks on disk and the weeks asked for (`span`), and a root whose weeks are not
contiguous -- a week that failed -- is left without a `summary.json` until a
later run fills the gap.

The id includes the strategy's revision, so a change to the strategy starts
a new folder: weeks of two versions are never put together.
"""

import datetime as dt
from dataclasses import dataclass

from qate.util.dt_range import DtRange

from . import run_dir
from .eval import SLIPPAGE_05, SLIPPAGE_10, evaluate_strategy
from .ledger import JST, Trade, streaks

DEFAULT_PRELOAD_HOURS = 24

# A summary's suffix, and the extra slippage rate it was evaluated at; see `eval.Evaluation`.
RATES = {"0": 0.0, "5": SLIPPAGE_05, "10": SLIPPAGE_10}


@dataclass
class WeeklySettings:
    """`enoshima.json`'s `"weekly"`. `preload_hours` is part of a weekly run's id: it changes the result."""

    preload_hours: int = DEFAULT_PRELOAD_HOURS

    def __post_init__(self):
        if not isinstance(self.preload_hours, int) or isinstance(self.preload_hours, bool) or self.preload_hours < 0:
            raise ValueError(f"weekly.preload_hours must be a whole number of hours, not {self.preload_hours!r}")


@dataclass(frozen=True)
class Week:
    """An ISO week in JST: Monday 00:00 to the next Monday 00:00, exclusive."""

    monday: dt.date

    @property
    def key(self) -> str:
        year, week, _ = self.monday.isocalendar()
        return f"{year}-W{week:02d}"

    @property
    def start(self) -> dt.datetime:
        return dt.datetime.combine(self.monday, dt.time(), JST)

    @property
    def dt_range(self) -> DtRange:
        """The reported range, ending a microsecond before the next Monday, as a date range ends."""
        return DtRange(self.start, self.start + dt.timedelta(days=7, microseconds=-1))

    def clip(self, first: dt.date, last: dt.date) -> DtRange | None:
        """The part of the week from day `first` to day `last`, inclusive, or None if they do not meet."""
        start = max(self.start, dt.datetime.combine(first, dt.time(), JST))
        end = min(self.dt_range.end_dt, dt.datetime.combine(last + dt.timedelta(days=1), dt.time(), JST)
                  - dt.timedelta(microseconds=1))
        return DtRange(start, end) if start <= end else None

    def replay_range(self, preload_hours: int) -> DtRange:
        return DtRange(self.start - dt.timedelta(hours=preload_hours), self.dt_range.end_dt)

    @classmethod
    def of(cls, ts: float) -> "Week":
        day = dt.datetime.fromtimestamp(ts, JST).date()
        return cls(day - dt.timedelta(days=day.weekday()))

    @classmethod
    def parse(cls, key: str) -> "Week":
        year, week = key.split("-W")
        return cls(dt.date.fromisocalendar(int(year), int(week), 1))


def covering(dt_range: DtRange) -> list[Week]:
    """The fewest weeks that contain `dt_range`, in order."""
    first, last = Week.of(dt_range.start_ts), Week.of(dt_range.end_ts)
    n = (last.monday - first.monday).days // 7
    return [Week(first.monday + dt.timedelta(weeks=i)) for i in range(n + 1)]


def summarize(trades: list[Trade], order_stats: dict) -> dict:
    """The figures `ResultReporter.get_result` reports, computed from closed trades and order counters.

    What makes weeks add up: every figure is a function of the trades in time
    order, or of the order counters, which are sums. One week's summary is this
    of its own trades; a window's is this of its weeks' trades concatenated and
    their counters added.
    """
    out: dict = {}
    for s, rate in RATES.items():
        changes = [getattr(t, f"pnl_change{s}") for t in trades]
        pnl = peak = drawdown = 0.0
        for change in changes:
            pnl += change
            peak = max(peak, pnl)
            drawdown = max(drawdown, peak - pnl)
        wins = [c for c in changes if c > 0.0]
        losses = [c for c in changes if c <= 0.0]
        orders = order_stats.get(f"order_cnt{s}", 0)
        result = {
            "pnl_cnt": len(changes),
            "max_drawdown": drawdown,
            "pnl": pnl,
            "fee": -sum(t.fee for t in trades),
            "slippage": 0.0 if orders == 0 else order_stats[f"order_slippage{s}"] / orders,
            "slippage_rate": 0.0 if orders == 0 else order_stats[f"order_slippage_rate{s}"] / orders,
            "loss_avg": sum(losses) / len(losses) if losses else None,
            "loss_cnt": len(losses),
            "win_avg": sum(wins) / len(wins) if wins else None,
            "win_cnt": len(wins),
            "win_rate": len(wins) * 100 / len(changes) if changes else 0.0,
        }
        result.update(evaluate_strategy([(t.close_ts, c / t.open_cost * (1.0 + rate)) for t, c in zip(trades, changes)]))
        out.update({f"{k}{s}": v for k, v in result.items()})
    out.update(streaks(trades))
    return out


def add_order_stats(total: dict, week: dict) -> None:
    for s in RATES:
        for name in ("order_cnt", "order_slippage", "order_slippage_rate"):
            total[f"{name}{s}"] = total.get(f"{name}{s}", 0) + week.get(f"{name}{s}", 0)


def on_disk(root, run_id: str) -> list[Week]:
    """The weeks this run has a finished result for, in order."""
    base = run_dir.run_dir(root, run_id) / run_dir.WEEKS_DIR
    if not base.is_dir():
        return []
    return sorted(
        (Week.parse(p.name) for p in base.iterdir() if "." not in p.name and run_dir.is_complete(p)),
        key=lambda w: w.monday,
    )


def span(weeks: list[Week], done: list[Week]) -> list[Week]:
    """Every week from the first to the last of `weeks` and `done`: what keeps a run's weeks contiguous."""
    both = weeks + done
    first, last = min(both, key=lambda w: w.monday), max(both, key=lambda w: w.monday)
    return [Week(first.monday + dt.timedelta(weeks=i)) for i in range((last.monday - first.monday).days // 7 + 1)]


def recorded_range(root, run_id: str, week: Week) -> tuple[str, str]:
    """The range a week on disk was run over: the whole week, or the part that had data then."""
    meta = run_dir.read_json(run_dir.week_dir(root, run_id, week.key), run_dir.META)
    return meta["start_str"], meta["end_str"]


def is_contiguous(weeks: list[Week]) -> bool:
    return all((b.monday - a.monday).days == 7 for a, b in zip(weeks, weeks[1:]))


def rebuild(root, run_id: str, meta: dict) -> bool:
    """Rewrite the run's root files from every week on disk. False, and no root summary, if they have a gap.

    `meta` is what the weeks share (`BacktestLog.weekly_meta`); the range is the weeks'.
    """
    weeks = on_disk(root, run_id)
    if not weeks or not is_contiguous(weeks):
        run_dir.remove_summary(root, run_id)
        return False
    trades, summary = window(root, run_id, weeks)
    first = run_dir.read_json(run_dir.week_dir(root, run_id, weeks[0].key), run_dir.META)
    last = run_dir.read_json(run_dir.week_dir(root, run_id, weeks[-1].key), run_dir.META)
    meta = {
        **meta,
        "start_str": first["start_str"],   # a partial first or last week's own
        "end_str": last["end_str"],
        "weeks": len(weeks),
    }
    run_dir.write_cumulative(root, run_id, meta, summary, trades)
    return True


def missing(root, run_id: str, weeks: list[Week]) -> list[Week]:
    """The weeks of `weeks` that this run has no finished result for."""
    return [w for w in weeks if not run_dir.is_complete(run_dir.week_dir(root, run_id, w.key))]


def window(root, run_id: str, weeks: list[Week]) -> tuple[list[Trade], dict]:
    """A window's trades and summary, from its weeks' folders. Replays nothing.

    Every week must be there: a window short of one is not that window's result.
    """
    absent = missing(root, run_id, weeks)
    if absent:
        raise ValueError(f"Run {run_id} has no result for {', '.join(w.key for w in absent)}")
    trades: list[Trade] = []
    order_stats: dict = {}
    for w in weeks:
        path = run_dir.week_dir(root, run_id, w.key)
        trades.extend(run_dir.read_trades(path))
        add_order_stats(order_stats, run_dir.read_json(path, run_dir.SUMMARY))
    return trades, summarize(trades, order_stats)
