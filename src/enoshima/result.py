"""What a backtest computes about itself: the evaluations, and the result.

`ResultReporter` is the reporter every backtest has. It evaluates each fill and
each closed position, and its `get_result` is the figure a run log records and
`--optimize` ranks. It also writes per-trade PnL and strategy bars to parquet
through `timeseries`, beside the run.

It does not record the metric stream. That is `influx.InfluxReporter`, added only
when a run asks for it, so a backtest without a database still finishes with its
result. The evaluations' own `EVAL` metrics are published back through the
runtime, which hands them to whichever reporters are recording.

## The three slippage evaluations

Every run is evaluated three times, at 0, 0.005% and 0.01% extra slippage, and
the results are suffixed `0`, `5` and `10`. One number is not a result: a
strategy whose edge survives at 0 and vanishes at 0.005% has told you something
important about itself, and reporting only the optimistic figure hides it.
"""

from logging import getLogger
from pathlib import Path

from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, Measurement, Metric, SettleType
from qate.core.order import OrderResponse
from qate.core.symbol import Symbol
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.util.dt_range import DtRange

from .eval import Evaluation
from .timeseries import ResultStore

LOG = getLogger(__name__)

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
    """Evaluates a backtest and writes its PnL and bars to its own directory."""

    def __init__(
        self,
        result_dir: str | Path,
        dt_range: DtRange,
        strategy_name: str,
        param_set_id: str,
    ):
        super().__init__()
        self.result_dir = Path(result_dir)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.dt_range = dt_range
        self.strategy_name = strategy_name
        self.param_set_id = param_set_id

        self.store = ResultStore(self.result_dir, strategy_name, param_set_id)
        self.pnl_stores: dict[str, object] = {}
        self.bar_stores: dict[str, object] = {}

        self.evals = [Evaluation(), Evaluation(SLIPPAGE_05), Evaluation(SLIPPAGE_10)]
        self._cum_pnl = 0.0
        self._peak_equity = 0.0

    def report_metrics(self, metrics: list[Metric]) -> None:
        for metric in metrics:
            if metric.measurement == Measurement.BAR.value and in_range(metric.ts, self.dt_range):
                self._add_bar(metric)

    def _add_bar(self, metric: Metric) -> None:
        market = Market(ExchangeName(metric.tags[1]), Symbol(metric.tags[3]))
        store = self.bar_stores.get(market.id)
        if store is None:
            store = self.store.create_bar(market)
            self.bar_stores[market.id] = store
        fields = dict(zip(metric.fields[::2], metric.fields[1::2]))
        store.add(metric.ts, fields)

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

        pnl_inc = pnl_update.pnl - pnl_update.fee
        self._cum_pnl += pnl_inc
        self._peak_equity = max(self._peak_equity, self._cum_pnl)
        drawdown = self._peak_equity - self._cum_pnl
        self._add_pnl(pnl_update, pnl_inc, drawdown)

        # For comparison with live trading: whoever records the run gets these
        # through the runtime, as if the strategy had emitted them.
        self.publish_status(EventType.METRICS, eval_metrics)

    def _add_pnl(self, pnl_update: PnlUpdate, pnl_inc: float, drawdown: float) -> None:
        market = pnl_update.market
        store = self.pnl_stores.get(market.id)
        if store is None:
            store = self.store.create_pnl(market)
            self.pnl_stores[market.id] = store
        # Keyed on open_ts: a position belongs to when it was entered, which is
        # what makes a PnL series comparable with the signal that caused it.
        store.add(
            pnl_update.open_ts,
            {
                "pnl_inc": pnl_inc,
                "side": pnl_update.side.value,
                "pnl_return": pnl_update.pnl_return,
                "close_ts": pnl_update.get_ts(),
                "open_cost": pnl_update.open_cost,
                "notional": pnl_update.notional,
                "close_interval": pnl_update.close_interval,
                "equity": self._cum_pnl,
                "drawdown": drawdown,
            },
        )

    def get_result(self) -> dict:
        """Every evaluation's numbers, suffixed by its slippage rate."""
        result: dict = {}
        for evaluation in self.evals:
            result.update(evaluation.get_result())
        return result

    def stop(self) -> None:
        for store in list(self.pnl_stores.values()) + list(self.bar_stores.values()):
            store.close()
        LOG.info(f"Results written to {self.result_dir}")
