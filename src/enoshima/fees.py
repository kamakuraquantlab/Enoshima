"""What a backtest's fills cost: `enoshima.json`'s `"fee_rates"`.

qate costs a fill with the rate of its market, and finds that rate on the
market's `ExchangeAdapter`, which a venue package registers when it is
installed. A backtest host installs none -- a venue package is a venue client,
and a backtest must not be able to reach an exchange -- so without this every
fill of every backtest was costed at zero, and the only sign was a warning per
tracker in `env.log`. On a margin market that charges nothing that is right; on
a spot market with a taker fee it overstates every result.

So the env states its rates, by market id:

```json
"fee_rates": {"GMO:XRP_JPY": {"maker": 0.0, "taker": 0.0}}
```

and `register` hands them to qate as a fee-only adapter (no API, no
connection), which is where `PnlTracker.register_fee` looks. A rate is a
fraction of notional; a negative maker rate is a rebate. Zero is written out,
not left absent: "this market charges nothing" and "nobody said" must not look
alike.

`check` refuses a run whose strategy trades a market without a rate, before
anything is replayed. A market is traded when its exchange is one of the
config's `get_exchanges()`; a market read only for its data, such as a
reference price on another venue, needs no rate.

A rate changes what a run computes, so the traded markets' rates are part of
its id (`identity`) -- except a zero one, which computes what a run made
before this file had a rate for it, and keeps that run's folder.
"""

from dataclasses import replace
from logging import getLogger

from qate.core.model import Market
from qate.exchange import FeeSchedule, registry

LOG = getLogger(__name__)


def parse(data, where: str) -> dict[str, FeeSchedule]:
    """`{"EXCHANGE:SYMBOL": {"maker": m, "taker": t}}` as rates by market id; anything else refused."""
    if not isinstance(data, dict):
        raise ValueError(f'"fee_rates" in {where} must be an object, e.g. {{"GMO:XRP_JPY": {{"maker": 0.0, "taker": 0.0}}}}')
    rates = {}
    for market_id, rate in data.items():
        try:
            market = Market.from_str(market_id)
        except (KeyError, IndexError):
            raise ValueError(f'"fee_rates" in {where}: {market_id!r} is not a market id, e.g. "GMO:XRP_JPY"') from None
        if not isinstance(rate, dict) or set(rate) != {"maker", "taker"}:
            raise ValueError(f'"fee_rates" in {where}: {market_id} needs exactly "maker" and "taker"')
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in rate.values()):
            raise ValueError(f'"fee_rates" in {where}: {market_id}\'s rates must be numbers')
        rates[market.id] = FeeSchedule(maker=float(rate["maker"]), taker=float(rate["taker"]))
    return rates


def traded_markets(config) -> list[Market]:
    """The config's markets on an exchange it trades on, in its order."""
    exchanges = set(config.get_exchanges())
    seen, markets = set(), []
    for market, _ in config.get_markets():
        if market.exchange_name in exchanges and market.id not in seen:
            seen.add(market.id)
            markets.append(market)
    return markets


def check(rates: dict[str, FeeSchedule], config) -> dict[str, FeeSchedule]:
    """The traded markets' rates; a traded market without one refuses the run."""
    markets = traded_markets(config)
    missing = [m.id for m in markets if m.id not in rates]
    if missing:
        example = ", ".join(f'"{m}": {{"maker": 0.0, "taker": 0.0}}' for m in missing)
        raise ValueError(
            f"No fee rate for {', '.join(missing)}, which the strategy trades. Add it to "
            f'enoshima.json\'s "fee_rates", e.g. {{{example}}}, with the venue\'s real rates'
        )
    return {m.id: rates[m.id] for m in markets}


def register(rates: dict[str, FeeSchedule]) -> None:
    """Give qate these rates, on the adapter of each market's exchange."""
    by_exchange: dict = {}
    for market_id, rate in rates.items():
        market = Market.from_str(market_id)
        by_exchange.setdefault(market.exchange_name, {})[market.symbol] = rate
    for exchange_name, schedules in by_exchange.items():
        adapter = registry.find(exchange_name)
        if adapter is None:
            adapter = registry.ExchangeAdapter(exchange_name=exchange_name, fee_rates=schedules)
        else:
            adapter = replace(adapter, fee_rates={**adapter.fee_rates, **schedules})
        registry.register(adapter)
    for market_id, rate in rates.items():
        LOG.info(f"Fee rate for {market_id}: maker={rate.maker} taker={rate.taker}")


def identity(rates: dict[str, FeeSchedule]) -> dict:
    """The rates as they go into a run's id: the non-zero ones only."""
    return {
        market_id: {"maker": rate.maker, "taker": rate.taker}
        for market_id, rate in sorted(rates.items())
        if rate.maker or rate.taker
    }


def to_json(rates: dict[str, FeeSchedule]) -> dict:
    """The rates as a run's meta records them, zero ones included."""
    return {market_id: {"maker": rate.maker, "taker": rate.taker} for market_id, rate in sorted(rates.items())}
