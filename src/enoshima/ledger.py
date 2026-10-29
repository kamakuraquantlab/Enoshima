"""A run's closed trades, and the same trades grouped by time and by kind.

`ResultReporter` keeps one `Trade` per closed position inside the range, with
the PnL change it made at each slippage rate. Everything else of a run's result
that is not provenance is computed from that list: the whole-range summary is
`Evaluation`'s, and every grouping (`by_<name>`) is `aggregate` over one of `GROUPINGS`.

## A group's row

`trades`, and per slippage suffix N: `winsN` (trades whose change was
positive), `pnlN`, and `peakN`, `troughN` and `ddN` -- the high, the low and
the worst drawdown of the group's own PnL, starting from zero and taking its
trades in time order.

For a grouping that follows the calendar (month, week, day) that is the
group's equity curve from its start, and consecutive groups combine exactly --
`selection.window_stats` recomputes any run of months' drawdown from them. For
one that collects trades from scattered times (weekday, hour, holding time,
side) it is the curve of a strategy that traded only in that group: useful, and
not combinable across groups.

## Time

JST, the same day boundary as bronze's `date=` partitions.
"""

import datetime as dt
from dataclasses import dataclass

JST = dt.timezone(dt.timedelta(hours=9))

SUFFIXES = ("0", "5", "10")

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Upper bounds in seconds, and the label of the bucket below each.
HOLDING_BUCKETS = (
    (60, "<1m"),
    (300, "1-5m"),
    (1800, "5-30m"),
    (7200, "30m-2h"),
    (28800, "2-8h"),
    (float("inf"), ">8h"),
)


@dataclass
class Trade:
    """One closed position: what the simulator reported, and what it did to each evaluation."""

    close_ts: float
    open_ts: float | None
    exchange: str
    symbol: str
    side: str  # the position's: "long" or "short"
    notional: float
    open_cost: float
    pnl: float  # realised, before fees and extra slippage
    fee: float  # the round trip's
    pnl_change0: float
    pnl_change5: float
    pnl_change10: float
    # The closing fill's execution price. Last, and optional, so a trades.csv
    # written before it was recorded still reads.
    close_price: float | None = None

    @property
    def holding_s(self) -> float | None:
        return None if self.open_ts is None else self.close_ts - self.open_ts

    def jst(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.close_ts, JST)


TRADE_COLUMNS = list(Trade.__dataclass_fields__)


def month_of(ts: float) -> str:
    """The JST month a timestamp falls in, as `YYYY-MM`."""
    return dt.datetime.fromtimestamp(ts, JST).strftime("%Y-%m")


def holding_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    for bound, label in HOLDING_BUCKETS:
        if seconds < bound:
            return label
    return HOLDING_BUCKETS[-1][1]


# name -> (key of a trade, every key in display order or None to sort what occurs)
GROUPINGS = {
    "month": (lambda t: t.jst().strftime("%Y-%m"), None),
    "week": (lambda t: t.jst().strftime("%G-W%V"), None),
    "day": (lambda t: t.jst().strftime("%Y-%m-%d"), None),
    "weekday": (lambda t: WEEKDAYS[t.jst().weekday()], WEEKDAYS),
    "monthday": (lambda t: f"{t.jst().day:02d}", tuple(f"{d:02d}" for d in range(1, 32))),
    "hour": (lambda t: f"{t.jst().hour:02d}", tuple(f"{h:02d}" for h in range(24))),
    "weekday_hour": (
        lambda t: f"{WEEKDAYS[t.jst().weekday()]} {t.jst().hour:02d}",
        tuple(f"{d} {h:02d}" for d in WEEKDAYS for h in range(24)),
    ),
    "holding": (lambda t: holding_bucket(t.holding_s), tuple(label for _, label in HOLDING_BUCKETS) + ("unknown",)),
    "side": (lambda t: t.side, ("long", "short")),
}


def empty_row() -> dict:
    row = {"trades": 0}
    for s in SUFFIXES:
        row.update({f"wins{s}": 0, f"pnl{s}": 0.0, f"peak{s}": 0.0, f"trough{s}": 0.0, f"dd{s}": 0.0})
    return row


def add_to_row(row: dict, trade: Trade) -> None:
    row["trades"] += 1
    for s in SUFFIXES:
        change = getattr(trade, f"pnl_change{s}")
        pnl = row[f"pnl{s}"] = row[f"pnl{s}"] + change
        if change > 0:
            row[f"wins{s}"] += 1
        row[f"peak{s}"] = max(row[f"peak{s}"], pnl)
        row[f"trough{s}"] = min(row[f"trough{s}"], pnl)
        row[f"dd{s}"] = max(row[f"dd{s}"], row[f"peak{s}"] - pnl)


def aggregate(trades: list[Trade], name: str) -> dict[str, dict]:
    """One grouping's rows, keyed in display order. `trades` must be in time order.

    A grouping with a fixed set of keys (weekday, hour, ...) lists every key,
    traded or not, so a chart of it has no holes; a calendar one lists only
    the periods that traded.
    """
    key_of, keys = GROUPINGS[name]
    rows: dict[str, dict] = {}
    for trade in trades:
        key = key_of(trade)
        row = rows.get(key)
        if row is None:
            row = rows[key] = empty_row()
        add_to_row(row, trade)
    if keys is None:
        return dict(sorted(rows.items()))
    return {key: rows.get(key) or empty_row() for key in keys if key in rows or key != "unknown"}


def streaks(trades: list[Trade]) -> dict:
    """The longest runs of losing and of winning trades, at each slippage rate."""
    out = {}
    for s in SUFFIXES:
        longest = {"win": 0, "loss": 0}
        current, kind = 0, None
        for trade in trades:
            this = "win" if getattr(trade, f"pnl_change{s}") > 0 else "loss"
            current = current + 1 if this == kind else 1
            kind = this
            longest[kind] = max(longest[kind], current)
        out[f"max_win_streak{s}"] = longest["win"]
        out[f"max_loss_streak{s}"] = longest["loss"]
    return out
