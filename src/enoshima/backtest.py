"""One strategy, one parameter set, one replay.

`BacktestRuntime` is the backtest's counterpart of `qate.trading.runtime.Runtime`:
it drives a `Trader`, stands a `BacktestGateway` where an exchange gateway would
be, and hands every outcome to its `Reporter`s through the same hooks a live run
uses -- `report_order`, `report_metrics`, `report_pnl`. What records a run is
therefore a choice of reporters, not a property of the runtime:

```
Channel ──► Trader ──► Strategy ──► BacktestGateway
 (market     (event      (creates    (matches against
  events)     loop)       orders)     the next book)
                 │                         │
                 └──── BacktestRuntime ◄───┘
                          │ report_order / report_metrics / report_pnl
                          ├──► ResultReporter   evaluations, the run's result (always)
                          └──► InfluxReporter   metrics, orders, PnL (if enoshima.json asks)
```

It differs from `Runtime` where a replay differs from a live run:

- **No thread and no queue of its own.** It is the `EventQueue` the trader and
  gateways publish into, and it dispatches inside the caller's call, so a replay
  stays deterministic.
- **Any exception fails the run, and stops it.** A live runtime logs and carries
  on, because a run must not depend on anyone being told. A backtest's reporters
  *are* its output, and a result computed past an exception is not a result. qate's
  event loop catches what a handler raises -- the strategy's or a reporter's -- and
  publishes it as `EXCEPTION`; the runtime stops its trader at the first one, and
  `Backtest.run` raises, so the run is reported failed and its log is not saved.

A strict run -- a week of a weekly run -- opens nothing past its range and
stops early. Its overlap is there only to see the week's trades closed, so

- an open order made in the overlap is taken and never filled
  (`OverlapGateway`), as a resting order the market never reaches. Filled, it
  could add to a position of the week's, and qate's `open_ts` -- the latest
  open's -- would make the whole position the next week's. Refused instead, it
  would be an order error, and qate's `Strategy` stops trading at the second
  open that fails to create, which would fail the run;
- the replay ends at the first market event past the range at which none of
  the week's positions is open or about to be (`ResultReporter.settled`).
  The strategy does not see that event, and the channel is closed as on any
  other exit.

Reporters are terminal sinks. Evaluation results write directly through the
optional InfluxDB writer owned by the run; they do not masquerade as strategy
metrics and re-enter this dispatch.
"""

from logging import getLogger

from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.model import EventLoopExit, ExchangeName, SettleType
from qate.core.order import OrderRequest
from qate.trading.gateways import BacktestGateway
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

from .influx import UNRECORDED_MEASUREMENTS
from .result import ResultReporter
from .run_log import BacktestLog

LOG = getLogger(__name__)


class BacktestFailed(Exception):
    """A replay raised. Its numbers are not a result."""


class OverlapGateway(BacktestGateway):
    """A `BacktestGateway` that, past `opens_until`, takes open orders and never fills them.

    The strategy is told the order was created, as for any resting order; a
    later cancel of it finds nothing to cancel, which qate's gateway already
    treats as an ordinary race rather than an error.
    """

    def __init__(self, exchange_name: ExchangeName, opens_until: float):
        super().__init__(exchange_name)
        self.opens_until = opens_until

    def create(self, order_request: OrderRequest):
        if order_request.settle_type != SettleType.OPEN or order_request.get_ts() <= self.opens_until:
            super().create(order_request)
            return
        order_tracker = self.matching.create(order_request)
        del self.matching.orders[order_request.ctx_id]
        LOG.debug(f"Holding open order {order_request.ctx_id} made in the overlap: it will not fill")
        self.publish_order_created(order_tracker.created_response())


class BacktestRuntime(EventQueue):
    _DISPATCH = {
        EventType.TRADING_ORDER: "report_order",
        EventType.TRADING_METRICS: "report_metrics",
        EventType.TRADING_PNL_UPDATE: "report_pnl",
        EventType.EXCEPTION: "report_exception",
    }

    def __init__(
        self,
        strategy: Strategy,
        exchanges: list[ExchangeName],
        channel: EventQueue,
        opens_until: float | None = None,
    ):
        """`opens_until`, given, is when open orders stop filling: see `OverlapGateway`."""
        self.channel = channel
        # Nothing records these (see `influx.UNRECORDED_MEASUREMENTS`); a
        # strategy that asks `wants_metric` need not build them. A copy per
        # strategy, built from a tuple because `frozenset(a_frozenset)` hands
        # back the same object: one set read by every thread of a sweep, for
        # every event, made the whole sweep 2.4x slower on free-threaded Python.
        strategy.skip_measurements = frozenset(tuple(UNRECORDED_MEASUREMENTS))
        # A run's parameters are the set it was built with. A wall-clock reload
        # would replace a sweep member partway through a deterministic replay.
        self.trader = Trader(strategy, channel, reload_params=False)
        self.reporters: list[Reporter] = []
        self.gateways: list[BacktestGateway] = []
        self._settled = None

        for exchange_name in exchanges:
            if opens_until is None:
                gateway = BacktestGateway(exchange_name)
            else:
                gateway = OverlapGateway(exchange_name, opens_until)
            gateway.add_order_listener(self)
            self.trader.add_gateway(gateway)
            self.trader.register(EventType.MARKET_ORDER_BOOK, gateway.handle_order_book)
            self.gateways.append(gateway)

        # After the gateways, which may fill a last close on this book, and
        # before the strategy, which registers its handlers once warmed up.
        for event_type in (EventType.MARKET_ORDER_BOOK, EventType.MARKET_TRADE):
            self.trader.register(event_type, self._stop_when_settled)

        self.trader.add_status_listener(self)

    def stop_when(self, settled) -> None:
        """End the replay at the first market event for which `settled(ts, self.pending_opens)` is true."""
        self._settled = settled

    def pending_opens(self) -> list:
        """The open orders made and not yet filled, on every gateway."""
        return [
            tracker.order_request
            for gateway in self.gateways
            for tracker in gateway.matching.orders.values()
            if tracker.order_request.settle_type == SettleType.OPEN
        ]

    def _stop_when_settled(self, event) -> None:
        if self._settled is not None and self._settled(event.get_ts(), self.pending_opens):
            raise EventLoopExit()

    def add_reporter(self, reporter: Reporter) -> None:
        self.reporters.append(reporter)

    def put(self, data) -> None:
        if data is None:
            return
        (event_type, event) = data
        call = self._DISPATCH.get(event_type)
        if call is None:
            return
        for reporter in self.reporters:
            getattr(reporter, call)(event)
        if event_type == EventType.EXCEPTION:
            # Replaying on would only compute a result nobody should use.
            self.trader.stop()

    def run(self) -> None:
        try:
            for reporter in self.reporters:
                reporter.start()
            try:
                self.trader.run()
            finally:
                for reporter in self.reporters:
                    reporter.stop()
        finally:
            # However this ends, the other backtests must stop waiting for this one.
            close = getattr(self.channel, "close", None)
            if close is not None:
                close()

    def stop(self) -> None:
        self.trader.stop()


class Backtest:
    """A runtime, the reporter that computes its result, and the log that records it."""

    def __init__(
        self,
        strategy: Strategy,
        exchanges: list[ExchangeName],
        channel: EventQueue,
        result: ResultReporter,
        run_log: BacktestLog,
        reporters: list[Reporter] = (),
    ):
        self.result = result
        self.run_log = run_log
        opens_until = result.dt_range.end_ts if result.strict else None
        self.runtime = BacktestRuntime(strategy, exchanges, channel, opens_until)
        self.runtime.add_reporter(result)
        if result.strict:
            self.runtime.stop_when(result.settled)
        for reporter in reporters:
            self.runtime.add_reporter(reporter)

    def run(self) -> None:
        self.runtime.run()
        if self.result.exceptions:
            first = self.result.exceptions[0]
            raise BacktestFailed(
                f"{len(self.result.exceptions)} exception(s) during the replay; the first: {first!r}"
            ) from first
        self.run_log.set_result(self.result.get_result())

    def stop(self) -> None:
        self.runtime.stop()
