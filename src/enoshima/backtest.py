"""One strategy, one parameter set, one replay.

`BacktestRuntime` is the backtest's counterpart of `qate.trading.runtime.Runtime`:
it drives a `Trader`, stands a `SimulatorGateway` where an exchange gateway would
be, and hands every outcome to its `Reporter`s through the same hooks a live run
uses -- `report_order`, `report_metrics`, `report_pnl`. What records a run is
therefore a choice of reporters, not a property of the runtime:

```
Channel ──► Trader ──► Strategy ──► SimulatorGateway
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

Reporters are terminal sinks. Evaluation results write directly through the
optional InfluxDB writer owned by the run; they do not masquerade as strategy
metrics and re-enter this dispatch.
"""

from logging import getLogger

from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName
from qate.trading.gateways import SimulatorGateway
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

from .influx import UNRECORDED_MEASUREMENTS
from .result import ResultReporter
from .run_log import BacktestLog

LOG = getLogger(__name__)


class BacktestFailed(Exception):
    """A replay raised. Its numbers are not a result."""


class BacktestRuntime(EventQueue):
    _DISPATCH = {
        EventType.TRADING_ORDER: "report_order",
        EventType.TRADING_METRICS: "report_metrics",
        EventType.TRADING_PNL_UPDATE: "report_pnl",
        EventType.EXCEPTION: "report_exception",
    }

    def __init__(self, strategy: Strategy, exchanges: list[ExchangeName], channel: EventQueue):
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

        for exchange_name in exchanges:
            gateway = SimulatorGateway(exchange_name)
            gateway.add_order_listener(self)
            self.trader.add_gateway(gateway)
            self.trader.register(EventType.MARKET_ORDER_BOOK, gateway.handle_order_book)

        self.trader.add_status_listener(self)

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
        self.runtime = BacktestRuntime(strategy, exchanges, channel)
        self.runtime.add_reporter(result)
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
