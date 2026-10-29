"""Strict weekly mode: a backtest computed a week at a time, so a longer one is built from weeks.

`enoshima run --weekly` widens the requested range to the ISO weeks that cover
it, Monday 00:00 to Sunday 24:00 JST, and replays each week on its own: a fresh
strategy and simulated exchange, fed `preload_days` before the Monday and
`overlap_days` after the Sunday. A trade belongs to the week it was *opened*
in. For 2026-W41, 10-05 to 10-11, the replay is 10-04 to 10-12:

    10-04          10-05 ... 10-11          10-12
    preload        the week                 overlap
    warms up the   every trade opened       closes what the week opened;
    indicators     here is the week's       its own opens are the next week's

A trade opened in the preload is the previous week's, and one opened in the
overlap the next week's: the overlap day is the next week's own Monday, and
each trade is counted by exactly one week. An open order made in the overlap
is taken and never filled (`backtest.OverlapGateway`): filled, it could add to
a position the week opened, and make it look opened in the overlap. The orders counted -- behind the
slippage averages, and in the lake's `BacktestOrder` -- are those trades' legs, wherever they
filled, so they too add up across weeks. The replay ends as soon as the
overlap has closed the week's last position (`ResultReporter.settled`): what
a week reports is what happened to the trades it opened, and nothing else.

A week's result is then a function of the env, the strategy, the parameters,
the preload, the overlap and the week -- nothing else -- so

- a week computed for one window is the week any later window needs, and is
  skipped rather than replayed: a year extended by a month replays four weeks;
- a window's result is its weeks' trades, concatenated, and `summarize`d.

The week is the ISO week because `ledger`'s `by_week.json` already is, and
JST because bronze's day partitions are: a week is exactly seven files, and
its replay nine with the defaults. `by_week.json` groups by close, like every
`by_*.json`, so a trade opened on a Sunday and closed on the Monday is in the
next week's row there and in its own week's part.

## What it does not promise

Equality with one continuous replay of the same window. Each week starts
cold, so a position opened before the preload and still open on the Monday is
in neither week, and a strategy whose state needs more history than the
preload diverges from the continuous run. A trade the week opened and the
overlap did not see closed is in no week either. `open_at_end` in a week's
summary counts those: the week's own positions still open when its replay
ended. A strategy that holds for longer than the overlap needs a longer one.

## The run is cumulative

One run id per environment, strategy revision, parameter set, preload and
overlap -- no dates in it -- and every weekly run of them adds to it, in the
lake (`lake`):

    BacktestRun   run_id=<id>                 rebuilt from every week on disk (`rebuild`):
                                              an ordinary run over the weeks' range
    BacktestPart  run_id=<id>/part=<YYYY-Www> what a run writes, a week at a time: the
                                              week's provenance, batch and timings, and
                                              `summarize`'s figures plus the order counters
    BacktestTrade, BacktestOrder, ...         the week's trades and records, same partitions

## As much as there is data for

The weekly run is the main one, and is asked for ranges the data does not yet
reach -- to today, say. Each week is clipped to the complete days on disk,
the first to the last day every stream the strategy reads has, less the
overlap at the end: a day whose overlap is not on disk yet cannot have its
closes, and its opens wait for it. So the first and last weeks may be
partial, and a week with none of those days is not run. A
partial week is recorded with its own `start_str`/`end_str` and `partial`,
and is run again by any later weekly run whose data reaches further into it:
a week grows to full without `--force`. A day missing *between* the first and
last is not clipped; it replays as empty, with the usual warning.

The root is never the current run's result. It is the weeks', so its range is
the first week's Monday to the last week's Sunday, and `select`,
`walk-forward` and `results` read it like any other run. For that range
to be true the weeks must be contiguous: a run fills every gap between the
weeks on disk and the weeks asked for (`span`), and a run whose weeks are not
contiguous -- a week that failed -- is left without its `BacktestRun` row until
a later run fills the gap.

The id includes the strategy's revision, so a change to the strategy starts
a new run: weeks of two versions are never put together.
"""

import datetime as dt
from dataclasses import dataclass

from qate.util.dt_range import DtRange

from .eval import SLIPPAGE_05, SLIPPAGE_10, evaluate_strategy
from .lake import WHOLE, Store
from .ledger import JST, Trade, streaks

DEFAULT_PRELOAD_DAYS = 1
DEFAULT_OVERLAP_DAYS = 1

# A summary's suffix, and the extra slippage rate it was evaluated at; see `eval.Evaluation`.
RATES = {"0": 0.0, "5": SLIPPAGE_05, "10": SLIPPAGE_10}


@dataclass
class WeeklySettings:
    """`enoshima.json`'s `"weekly"`. Both are part of a weekly run's id: they change the result.

    `preload_days` are replayed before the Monday to warm the strategy up;
    `overlap_days` after the Sunday, to close the trades the week opened.
    """

    preload_days: int = DEFAULT_PRELOAD_DAYS
    overlap_days: int = DEFAULT_OVERLAP_DAYS

    def __post_init__(self):
        for name in ("preload_days", "overlap_days"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"weekly.{name} must be a whole number of days, not {value!r}")

    @property
    def preload(self) -> dt.timedelta:
        return dt.timedelta(days=self.preload_days)

    @property
    def overlap(self) -> dt.timedelta:
        return dt.timedelta(days=self.overlap_days)


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

    def replay_range(self, settings: "WeeklySettings") -> DtRange:
        return DtRange(self.start - settings.preload, self.dt_range.end_dt + settings.overlap)

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


def on_disk(store: Store, run_id: str) -> list[Week]:
    """The weeks this run has a finished result for, in order."""
    return sorted((Week.parse(p) for p in store.parts(run_id) if p != WHOLE), key=lambda w: w.monday)


def span(weeks: list[Week], done: list[Week]) -> list[Week]:
    """Every week from the first to the last of `weeks` and `done`: what keeps a run's weeks contiguous."""
    both = weeks + done
    first, last = min(both, key=lambda w: w.monday), max(both, key=lambda w: w.monday)
    return [Week(first.monday + dt.timedelta(weeks=i)) for i in range((last.monday - first.monday).days // 7 + 1)]


def recorded_range(store: Store, run_id: str, week: Week) -> tuple[str, str]:
    """The range a week on disk was run over: the whole week, or the part that had data then."""
    meta = store.read_part(run_id, week.key)["meta"]
    return meta["start_str"], meta["end_str"]


def is_contiguous(weeks: list[Week]) -> bool:
    return all((b.monday - a.monday).days == 7 for a, b in zip(weeks, weeks[1:]))


def rebuild(store: Store, run_id: str, meta: dict) -> bool:
    """Rewrite the run's row from every week on disk. False, and no row, if they have a gap.

    `meta` is what the weeks share (`BacktestLog.weekly_meta`); the range is the weeks'.
    """
    weeks = on_disk(store, run_id)
    if not weeks or not is_contiguous(weeks):
        store.remove_run(run_id)
        return False
    trades, summary = window(store, run_id, weeks)
    first = store.read_part(run_id, weeks[0].key)["meta"]
    last = store.read_part(run_id, weeks[-1].key)["meta"]
    meta = {
        **meta,
        "start_str": first["start_str"],   # a partial first or last week's own
        "end_str": last["end_str"],
        "weeks": len(weeks),
    }
    store.write_run(run_id, meta, summary, trades)
    return True


def missing(store: Store, run_id: str, weeks: list[Week]) -> list[Week]:
    """The weeks of `weeks` that this run has no finished result for."""
    return [w for w in weeks if not store.has_part(run_id, w.key)]


def window(store: Store, run_id: str, weeks: list[Week]) -> tuple[list[Trade], dict]:
    """A window's trades and summary, from its weeks' parts. Replays nothing.

    Every week must be there: a window short of one is not that window's result.
    The trades are put in close order, as one replay reports them: a week's last
    can close, in its overlap, after the next week's first.
    """
    absent = missing(store, run_id, weeks)
    if absent:
        raise ValueError(f"Run {run_id} has no result for {', '.join(w.key for w in absent)}")
    trades: list[Trade] = []
    order_stats: dict = {}
    for w in weeks:
        trades.extend(store.read_part_trades(run_id, w.key))
        add_order_stats(order_stats, store.read_part(run_id, w.key)["summary"])
    trades.sort(key=lambda t: t.close_ts)
    return trades, summarize(trades, order_stats)
