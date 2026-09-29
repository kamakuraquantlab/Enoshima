"""The example strategy's configuration.

A strategy supplies its own `Config`, which is what `BootConfig` is for: it tells
the runner which venues to simulate and which markets and event types to replay,
and carries whatever else the strategy needs that is not a tunable parameter.

The split between this and `params` is worth getting right. Config is what makes
a run *a different run* — a different market, a different order size. Params are
what an optimize sweep varies. Putting a tunable in config means it cannot be
swept; putting a market in params means every combination reloads the data.
"""

from dataclasses import dataclass

from qate.boot.config import BootConfig
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market


@dataclass
class Config(BootConfig):
    market: Market
    order_size: float = 0.002

    def get_exchanges(self) -> list[ExchangeName]:
        return [self.market.exchange_name]

    def get_markets(self) -> list[tuple[Market, list[str]]]:
        # Order books only. This strategy decides from the touch, so it has no
        # use for the trade feed, and not loading it halves the replay.
        return [(self.market, [EventType.MARKET_ORDER_BOOK])]
