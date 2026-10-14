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
- **A reporter that raises fails the run.** A live runtime logs and carries on,
  because a run must not depend on anyone being told. A backtest's reporters *are*
  its output, and a result missing half its records is not a result.

A reporter may publish back into the runtime -- `Reporter` is a `StatusFeed` --
and what it publishes is dispatched to every reporter. That is how the
evaluations' own metrics reach InfluxDB without the two reporters knowing about
each other.
"""

from logging import getLogger

from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName
from qate.trading.gateways import SimulatorGateway
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

from .result import ResultReporter
from .run_log import BacktestLog

LOG = getLogger(__name__)


class BacktestRuntime(EventQueue):
    _DISPATCH = {
        EventType.ORDER_FILLED: "report_order",
        EventType.METRICS: "report_metrics",
        EventType.TRADING_PNL_UPDATE: "report_pnl",
        EventType.EXCEPTION: "report_exception",
        EventType.MSG_OUT: "report_message",
    }

    def __init__(self, strategy: Strategy, exchanges: list[ExchangeName], channel: EventQueue):
        # No params.json reload: a run's parameters are the set it was built
        # with, and the env's file would replace every sweep's set with its own.
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
        reporter.add_status_listener(self)

    def put(self, data) -> None:
        if data is None:
            return
        (event_type, event) = data
        call = self._DISPATCH.get(event_type)
        if call is None:
            return
        for reporter in self.reporters:
            getattr(reporter, call)(event)

    def run(self) -> None:
        for reporter in self.reporters:
            reporter.start()
        try:
            self.trader.run()
        finally:
            for reporter in self.reporters:
                reporter.stop()

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
        self.run_log.set_result(self.result.get_result())

    def stop(self) -> None:
        self.runtime.stop()
