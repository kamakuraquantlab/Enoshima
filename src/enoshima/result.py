"""What a backtest computes about itself: the evaluations, the trades, and the result.

`ResultReporter` is the reporter every backtest has. It evaluates each fill and
each closed position, and keeps the closed positions themselves as
`ledger.Trade`s. Its `get_result` is the run's summary -- the whole-range
figures a run folder's `summary.json` holds and `--optimize` filters on -- and
`trades` is what the folder's `trades.csv` and `by_*.json` are written from.

PnL is the one kind of trading data a run keeps on disk, because it is what a
result is evaluated from: with the trades in the folder, any grouping or window
-- `selection`'s walk-forward months among them -- is recomputed without a
replay. Orders and the strategy's bars go to InfluxDB through
`influx.InfluxReporter`, for looking at a run, and only when a run asks for it.
When InfluxDB recording is enabled, the evaluations write their own `EVAL`
metrics directly through the run's writer. They are results, not strategy
metrics, and do not re-enter the runtime's reporter dispatch.

## The three slippage evaluations

Every run is evaluated three times, at 0, 0.005% and 0.01% extra slippage, and
the results are suffixed `0`, `5` and `10`. One number is not a result: a
strategy whose edge survives at 0 and vanishes at 0.005% has told you something
important about itself, and reporting only the optimistic figure hides it.
"""

from qate.core.model import SettleType, Side
from qate.core.order import OrderResponse
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.util.dt_range import DtRange

from .eval import Evaluation
from .influx import InfluxdbWriter, convert_to_point
from .ledger import Trade, streaks

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

    def __init__(self, dt_range: DtRange, writer: InfluxdbWriter | None = None):
        super().__init__()
        self.dt_range = dt_range
        self.writer = writer
        self.evals = [Evaluation(), Evaluation(SLIPPAGE_05), Evaluation(SLIPPAGE_10)]
        # One per closed position inside the range, in time order.
        self.trades: list[Trade] = []
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

        eval_metrics = []
        changes = []
        for evaluation in self.evals:
            before = evaluation.overall_pnl
            eval_metrics.append(evaluation.on_close_pnl_update(pnl_update))
            changes.append(evaluation.overall_pnl - before)
        self.trades.append(
            Trade(
                close_ts=pnl_update.get_ts(),
                open_ts=pnl_update.open_ts,
                exchange=pnl_update.market.exchange_name.name,
                symbol=pnl_update.market.symbol.name,
                side="long" if pnl_update.side == Side.BUY else "short",
                notional=pnl_update.notional,
                open_cost=pnl_update.open_cost,
                pnl=pnl_update.pnl,
                fee=pnl_update.fee,
                pnl_change0=changes[0],
                pnl_change5=changes[1],
                pnl_change10=changes[2],
            )
        )

        if self.writer is not None:
            for metric in eval_metrics:
                self.writer.add(convert_to_point(metric))

    def get_result(self) -> dict:
        """Every evaluation's whole-range numbers, suffixed by its slippage rate."""
        result: dict = {}
        for evaluation in self.evals:
            result.update(evaluation.get_result())
        result.update(streaks(self.trades))
        return result
