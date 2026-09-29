"""One strategy, one parameter set, one replay.

The wiring is small and it is the whole of what makes a backtest run the same
code as a live trader: `Trader` drives the strategy, `SimulatorGateway` stands
where an exchange gateway would, and both get event queues that are synchronous
rather than threaded.

```
Channel ──► Trader ──► Strategy ──► SimulatorGateway ──► ResultCollector
 (market     (event      (creates    (matches against      (metrics, PnL,
  events)     loop)       orders)     the next book)         evaluations)
```

Nothing here is simulator-specific except the gateway. Swap it for a real one and
the same strategy trades.
"""

from logging import getLogger

from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName
from qate.trading.gateways import SimulatorGateway
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

from .result import ResultCollector
from .run_log import BacktestLog

LOG = getLogger(__name__)


class Backtest:
    def __init__(
        self,
        strategy: Strategy,
        exchanges: list[ExchangeName],
        channel: EventQueue,
        result: ResultCollector,
        run_log: BacktestLog,
    ):
        self.result = result
        self.run_log = run_log
        self.trader = Trader(strategy, channel)

        for exchange_name in exchanges:
            # No queue and no thread: the simulator answers inside the caller's
            # call. Slippage is 0 here because the evaluations apply their own
            # rates -- see result.ResultCollector.
            gateway = SimulatorGateway(exchange_name, slippage_rate=0.0)
            gateway.add_order_listener(self.result)
            self.trader.add_gateway(gateway)
            # Books reach the gateway through the trader's own loop, so the
            # gateway sees each snapshot before the strategy reacts to it.
            self.trader.register(EventType.MARKET_ORDER_BOOK, gateway.handle_order_book)

        self.trader.add_status_listener(self.result)

    def run(self) -> None:
        self.trader.run()
        self.result.close()
        self.run_log.set_result(self.result.get_result())
