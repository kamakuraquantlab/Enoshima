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
"""

from qate.core.ev_type import EventType
from qate.core.model import SettleType
from qate.core.order import OrderResponse
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.util.dt_range import DtRange

from .eval import Evaluation

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

        eval_metrics = [evaluation.on_close_pnl_update(pnl_update) for evaluation in self.evals]

        # For comparison with live trading: whoever records the run gets these
        # through the runtime, as if the strategy had emitted them.
        self.publish_status(EventType.METRICS, eval_metrics)

    def get_result(self) -> dict:
        """Every evaluation's numbers, suffixed by its slippage rate."""
        result: dict = {}
        for evaluation in self.evals:
            result.update(evaluation.get_result())
        return result
