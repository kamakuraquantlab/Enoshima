"""A moving-average crossover, as a worked example of the contract.

This is deliberately the most ordinary strategy there is. It is here to show the
shape a strategy has to take — the `Variant` class, warmup, deciding from an
event, placing an order, handing the fill to an inventory — not to make money. It
will not make money.

## What Enoshima loads

`trading.json` names `example.strategy` and variant `v1`, so the loader imports
`example.strategy.v1` and instantiates the class called `Variant`. That name is
the contract: exactly one `Variant` per file, constructed with `(config, params)`.

## The two rules that break a backtest silently

**Time comes from the event, never the clock.** `self.now_ts` is set from each
order book's timestamp. A strategy that calls `time.time()` still runs, still
produces numbers, and those numbers are wrong — it is comparing a 2026 wall clock
against replayed 2025 prices.

**Warmup is separate from trading.** `warmup_order_book` fills the price history
and nothing else; the run does not begin until `is_ready` is True. Deciding from
a half-full moving average would make the first hours of every backtest measure
an indicator that is still converging.

## Where to go next

Real strategies use `qate.trading.chart.BarChart` with the indicators in
`qate.trading.indicators`, so signals are computed on bars rather than on every
snapshot. A deque of mid prices is used here because it fits in one screen and
the point is the plumbing around it.
"""

from collections import deque
from logging import getLogger

from qate.core.gateway import ExchangeGateway
from qate.core.model import OrderBook, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse, OrderType
from qate.trading.inventory import SpotInventory
from qate.trading.strategy import Strategy

from .config import Config

LOG = getLogger(__name__)

# Starting balances for the simulated spot account. Generous on purpose: this
# example is about the mechanics, and a run that stops early because it ran out
# of quote currency would teach the wrong lesson.
START_QUOTE = 10_000_000.0
START_BASE = 0.0


class Variant(Strategy[Config]):
    def __init__(self, config: Config, params: dict):
        super().__init__(config, params)
        self.fast_period = int(params["fast_period"])
        self.slow_period = int(params["slow_period"])

        self.mids: deque[float] = deque(maxlen=self.slow_period)
        self.now_ts = 0.0
        self.gateway: ExchangeGateway = None
        self.inventory: SpotInventory = None
        self.position = 0.0
        self.order_in_flight = False
        self._was_above: bool | None = None

    def get_param_multiplier(self, name: str) -> int:
        # Periods are already integers. The default multiplier of 1000 would turn
        # a 60-bar period into "60000" in the param_set_id, which is the string
        # that names the results directory.
        return 1

    def add_gateway(self, gateway: ExchangeGateway) -> None:
        self.gateway = gateway

    def before_loop(self) -> None:
        self.inventory = SpotInventory(
            self.config.market,
            quote=START_QUOTE,
            base=START_BASE,
            max_drawdown=START_QUOTE,
        )

    # ------------------------------------------------------------------ warmup

    def warmup_order_book(self, order_book: OrderBook) -> None:
        self._record(order_book)

    @property
    def is_ready(self) -> bool:
        return len(self.mids) == self.slow_period

    # ------------------------------------------------------------------ trading

    def _record(self, order_book: OrderBook) -> None:
        mid = order_book.mid
        if mid is None:
            # An empty side happens in recorded data, around a collector resync.
            # Not an error; there is simply no mid to record.
            return
        self.now_ts = order_book.get_ts()
        self.mids.append(mid)

    def _sma(self, period: int) -> float:
        window = list(self.mids)[-period:]
        return sum(window) / len(window)

    def handle_order_book(self, order_book: OrderBook) -> None:
        self._record(order_book)
        if not self.is_ready or self.order_in_flight:
            return

        is_above = self._sma(self.fast_period) > self._sma(self.slow_period)
        was_above, self._was_above = self._was_above, is_above
        if was_above is None or was_above == is_above:
            return  # no crossing this snapshot

        if is_above and self.position == 0.0:
            self._order(Side.BUY, SettleType.OPEN, order_book)
        elif not is_above and self.position > 0.0:
            self._order(Side.SELL, SettleType.CLOSE, order_book)

    def _order(self, side: Side, settle_type: SettleType, order_book: OrderBook) -> None:
        price = order_book.best_ask if side == Side.BUY else order_book.best_bid
        if price is None:
            return
        if side == Side.BUY and not self.inventory.can_buy(self.config.order_size, price):
            LOG.warning(f"Not enough quote to buy: {self.inventory.log_str}")
            return

        self.order_in_flight = True
        self.gateway.create(
            OrderRequest(
                ts=self.now_ts,
                market=self.config.market,
                side=side,
                price=price,
                size=self.config.order_size,
                settle_type=settle_type,
                # DEFAULT is the taker path: cross the spread and be filled by
                # the next snapshot. A MAKER order would rest until the book
                # crossed it, which is a different strategy.
                order_type=OrderType.DEFAULT,
            )
        )

    # ------------------------------------------------------------------- fills

    def handle_order_filled(self, order_response: OrderResponse) -> None:
        super().handle_order_filled(order_response)
        self.order_in_flight = False

        if order_response.order_request.side == Side.BUY:
            self.position += order_response.exec_size
        else:
            self.position -= order_response.exec_size

        # The inventory turns a fill into realized PnL and a fee, and publishing
        # it is what puts the trade into the run's results. A strategy that does
        # not publish produces a backtest with no PnL at all.
        pnl_update = self.inventory.process_order(order_response)
        if pnl_update:
            pnl_update.market = self.config.market
            self.publish_pnl_update(pnl_update)

    def handle_order_error(self, order_response: OrderResponse) -> None:
        self.order_in_flight = False
        super().handle_order_error(order_response)

    def after_loop(self) -> None:
        super().after_loop()
        LOG.info(f"FINISHED position={self.position:.6f} {self.inventory.log_str}")
