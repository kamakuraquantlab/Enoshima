"""What a backtest computes about itself: the evaluations, and the result.

`ResultReporter` is the reporter every backtest has. It evaluates each fill and
each closed position, and its `get_result` is the figure a run log records and
`--optimize` ranks. It writes nothing else.

Trading data -- orders, per-trade PnL, the strategy's bars -- goes to InfluxDB
through `influx.InfluxReporter`, added only when a run asks for it, so a backtest
without a database still finishes with its result. There is no second, local copy
of that stream. The evaluations' own `EVAL` metrics are published back through
the runtime, which hands them to whichever reporters are recording.

## The three slippage evaluations

Every run is evaluated three times, at 0, 0.005% and 0.01% extra slippage, and
the results are suffixed `0`, `5` and `10`. One number is not a result: a
strategy whose edge survives at 0 and vanishes at 0.005% has told you something
important about itself, and reporting only the optimistic figure hides it.

## Per month

The result also holds `periods`: each evaluation's closed PnL split by JST
month (the same day boundary as bronze's partitions), with its trade count and
the high, low and worst drawdown of the PnL within the month, measured from the
month's start. That is enough to recompute any run of months -- its PnL and its
exact maximum drawdown -- without replaying again, which is what lets
`selection` choose parameters on some months and test them on the next.

And `days`: each JST day's closed trades and PnL at the three rates. That is
what relating a strategy to the market it traded needs -- a day's PnL beside
that day's volatility or trend -- and it comes from the run itself, not from
whatever a metrics database happens to hold.
"""

import datetime as dt

from qate.core.ev_type import EventType
from qate.core.model import SettleType
from qate.core.order import OrderResponse
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.util.dt_range import DtRange

from .eval import Evaluation

JST = dt.timezone(dt.timedelta(hours=9))


def month_of(ts: float) -> str:
    """The JST month a timestamp falls in, as `YYYY-MM`."""
    return dt.datetime.fromtimestamp(ts, JST).strftime("%Y-%m")


def day_of(ts: float) -> str:
    """The JST day a timestamp falls in, as `YYYY-MM-DD` -- bronze's `date=` partition."""
    return dt.datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")


class MonthLedger:
    """One evaluation's closed PnL, month by month, from the months' start."""

    def __init__(self, suffix: str):
        self.suffix = suffix
        self.months: dict[str, dict] = {}

    def add(self, month: str, pnl_change: float) -> None:
        m = self.months.get(month)
        if m is None:
            m = self.months[month] = {"trades": 0, "pnl": 0.0, "peak": 0.0, "trough": 0.0, "dd": 0.0}
        m["trades"] += 1
        m["pnl"] += pnl_change
        m["peak"] = max(m["peak"], m["pnl"])
        m["trough"] = min(m["trough"], m["pnl"])
        m["dd"] = max(m["dd"], m["peak"] - m["pnl"])

# Extra slippage rates the run is evaluated at, beyond the simulated fill.
# 0.005% is what live execution logs have actually shown for small orders in
# this niche; 0.01% is the pessimistic case. See Evaluation.on_order.
SLIPPAGE_05 = 0.00005
SLIPPAGE_10 = 0.0001


def in_range(ts: float, dt_range: DtRange) -> bool:
    """Whether an event is inside the reported range.

    A run is fed warmup data from before `dt_range.start_ts` so indicators have
    history. Those events are replayed but not reported: a PnL from the warmup
    period would be a trade the requested range did not contain.
    """
    return ts >= dt_range.start_ts


class ResultReporter(Reporter):
    """Evaluates a backtest at each slippage rate."""

    def __init__(self, dt_range: DtRange):
        super().__init__()
        self.dt_range = dt_range
        self.evals = [Evaluation(), Evaluation(SLIPPAGE_05), Evaluation(SLIPPAGE_10)]
        self.ledgers = [MonthLedger(evaluation.suffix) for evaluation in self.evals]
        self.days: dict[str, dict] = {}
        # What the event loop caught. Any at all and there is no result.
        self.exceptions: list[BaseException] = []

    def report_exception(self, error: BaseException) -> None:
        self.exceptions.append(error)

    def report_order(self, order_response: OrderResponse) -> None:
        # Evaluations see every fill, warmup included, so the reported slippage
        # average is over the whole replay rather than a truncated tail.
        for evaluation in self.evals:
            evaluation.on_order(order_response)

    def report_pnl(self, pnl_update: PnlUpdate) -> None:
        if not in_range(pnl_update.get_ts(), self.dt_range):
            return

        if pnl_update.settle_type == SettleType.OPEN:
            for evaluation in self.evals:
                evaluation.on_open_pnl_update(pnl_update)
            return

        month = month_of(pnl_update.get_ts())
        day = self.days.get(day_of(pnl_update.get_ts()))
        if day is None:
            day = self.days[day_of(pnl_update.get_ts())] = {"trades": 0, **{f"pnl{e.suffix}": 0.0 for e in self.evals}}
        day["trades"] += 1
        eval_metrics = []
        for evaluation, ledger in zip(self.evals, self.ledgers):
            before = evaluation.overall_pnl
            eval_metrics.append(evaluation.on_close_pnl_update(pnl_update))
            ledger.add(month, evaluation.overall_pnl - before)
            day[f"pnl{evaluation.suffix}"] += evaluation.overall_pnl - before

        # For comparison with live trading: whoever records the run gets these
        # through the runtime, as if the strategy had emitted them.
        self.publish_status(EventType.METRICS, eval_metrics)

    def get_result(self) -> dict:
        """Every evaluation's numbers, suffixed by its slippage rate."""
        result: dict = {}
        for evaluation in self.evals:
            result.update(evaluation.get_result())
        periods: dict[str, dict] = {}
        for ledger in self.ledgers:
            for month, m in ledger.months.items():
                row = periods.setdefault(month, {"trades": m["trades"]})
                for key in ("pnl", "peak", "trough", "dd"):
                    row[f"{key}{ledger.suffix}"] = m[key]
        result["periods"] = dict(sorted(periods.items()))
        result["days"] = dict(sorted(self.days.items()))
        return result
