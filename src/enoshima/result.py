"""Where one backtest's output goes: to disk, as it is produced.

The strategy publishes metrics, order fills and PnL updates as events. This is
what receives them.

Everything is written locally. Metric records go to a `qate.trading.metric_log`
exactly as the strategy emitted them, and per-trade PnL and strategy bars go to
parquet through `timeseries`. Nothing here contacts a database: exporting to
InfluxDB is `enoshima export`, afterwards, from these same files.

That ordering is deliberate and it replaced the opposite one. Writing straight to
InfluxDB during a run meant a backtest could not finish without a reachable
server, converting every metric to a database Point on the hot path, and losing
the whole run if the write failed at the end. Local files cost a fraction of that
and the export can be repeated.

## The three slippage evaluations

Every run is evaluated three times, at 0, 0.005% and 0.01% extra slippage, and
the results are suffixed `0`, `5` and `10`. One number is not a result: a
strategy whose edge survives at 0 and vanishes at 0.005% has told you something
important about itself, and reporting only the optimistic figure hides it.
"""

from logging import getLogger
from pathlib import Path

from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, Measurement, SettleType
from qate.core.order import OrderResponse
from qate.core.symbol import Symbol
from qate.trading.metric_log import MetricLog, MetricRecord, RotationInterval
from qate.trading.pnl_tracker import PnlUpdate
from qate.util.dt_range import DtRange

from .eval import Evaluation
from .timeseries import ResultStore

LOG = getLogger(__name__)

# Extra slippage rates the run is evaluated at, beyond the simulated fill.
# 0.005% is what live execution logs have actually shown for small orders in
# this niche; 0.01% is the pessimistic case. See Evaluation.on_order.
SLIPPAGE_05 = 0.00005
SLIPPAGE_10 = 0.0001

METRICS_PREFIX = "Metrics"
METRICS_FLUSH = 2048


class ResultCollector(EventQueue):
    """Receives a backtest's events and writes them to its own directory.

    Used as an `EventQueue` so the trader and gateways publish into it exactly as
    they would to any other listener. It only ever receives; `get` is never
    called.
    """

    # MARKET_PRICE and TRADE are emitted per market event. Keeping them would
    # make the metrics file larger than the bronze it was computed from, for data
    # that is already on disk in the bronze layer.
    _SKIP_MEASUREMENTS = {Measurement.MARKET_PRICE.value, Measurement.TRADE.value}

    def __init__(
        self,
        result_dir: str | Path,
        dt_range: DtRange,
        strategy_name: str,
        param_set_id: str,
    ):
        self.result_dir = Path(result_dir)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.dt_range = dt_range
        self.strategy_name = strategy_name
        self.param_set_id = param_set_id

        self.metrics_writer = MetricLog(
            RotationInterval.ONE_DAY,
            METRICS_FLUSH,
            str(self.result_dir / METRICS_PREFIX),
        )
        self.store = ResultStore(self.result_dir, strategy_name, param_set_id)
        self.pnl_stores: dict[str, object] = {}
        self.bar_stores: dict[str, object] = {}

        self.evals = [Evaluation(), Evaluation(SLIPPAGE_05), Evaluation(SLIPPAGE_10)]
        self._cum_pnl = 0.0
        self._peak_equity = 0.0

    def put(self, data) -> None:
        if data is None:
            return
        (event_type, event) = data
        if event_type == EventType.METRICS:
            self.handle_metrics(event)
        elif event_type == EventType.ORDER_FILLED:
            self.handle_order(event)
        elif event_type == EventType.TRADING_PNL_UPDATE:
            self.handle_pnl_update(event)

    def _should_emit(self, ts: float) -> bool:
        """Whether an event is inside the reported range.

        A run is fed warmup data from before `dt_range.start_ts` so indicators
        have history. Those events are replayed but not reported: a PnL from the
        warmup period would be a trade the requested range did not contain.
        """
        return ts >= self.dt_range.start_ts

    def _add_metric(self, metric_object: list) -> None:
        self.metrics_writer.add(MetricRecord(metric_object[1], metric_object))

    def handle_metrics(self, objects: list) -> None:
        for metrics in objects:
            # [measurement, ts, tags, fields]
            if metrics[0] in self._SKIP_MEASUREMENTS:
                continue
            if not self._should_emit(metrics[1]):
                continue
            self._add_metric(metrics)

            if metrics[0] == Measurement.BAR.value:
                self._add_bar(metrics)

    def _add_bar(self, metrics: list) -> None:
        market = Market(ExchangeName(metrics[2][1]), Symbol(metrics[2][3]))
        store = self.bar_stores.get(market.id)
        if store is None:
            store = self.store.create_bar(market)
            self.bar_stores[market.id] = store
        fields = dict(zip(metrics[3][::2], metrics[3][1::2]))
        store.add(metrics[1], fields)

    def handle_order(self, order_response: OrderResponse) -> None:
        # Evaluations see every fill, warmup included, so the reported slippage
        # average is over the whole replay rather than a truncated tail.
        for evaluation in self.evals:
            evaluation.on_order(order_response)

        if self._should_emit(order_response.get_ts()):
            self._add_metric(order_response.to_metric_object())

    def handle_pnl_update(self, pnl_update: PnlUpdate) -> None:
        if not self._should_emit(pnl_update.get_ts()):
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

        # The same close written at both ends of the position, tagged, so a chart
        # can align PnL either to when a trade was entered or to when it settled.
        for align, ts in (("close", pnl_update.get_ts()), ("open", pnl_update.open_ts)):
            metric_object = pnl_update.to_metric_object()
            metric_object[1] = ts
            metric_object[2].extend(["time_align", align])
            self._add_metric(metric_object)

        self._add_pnl(pnl_update, pnl_inc, drawdown)

        for metric_object in eval_metrics:
            self._add_metric(metric_object)

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

    def close(self) -> None:
        for store in list(self.pnl_stores.values()) + list(self.bar_stores.values()):
            store.close()
        self.metrics_writer.close()
        LOG.info(f"Results written to {self.result_dir}")
