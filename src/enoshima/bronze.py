"""Turning bronze data into the events a strategy sees.

Two vocabularies meet here, and this is the only place that speaks both.

Reading bronze is shared, so it is not done here. `komachi` writes the layer and
says where it is and which days are complete; `hase` reads a market-day into a
dataframe, for every tool in the ecosystem. Both take komachi's `"EX:SYM"`
strings. A second reader here -- globbing the tree, opening parquet itself --
would be a second implementation of one question, and the day the two disagree is
the day a backtest silently skips a date the downloader thinks it has.

`qate` owns the model: `OrderBook`, `Trade`, `EventType`. It knows nothing about
parquet, or about where data lives, and should not -- it is a trading library. And
`hase` knows nothing about `qate`, because an analysis tool must not depend on
trading code.

So Enoshima does only the join: ask hase for a frame, hand back qate objects.

## The two datasets

`Trade` is `ts, side, price, size`, with `side` 0 for a buy and 1 for a sell.
`OrderBook` is `ts` plus `bid<i>_price, bid<i>_qty` and `ask<i>_price,
ask<i>_qty` per level.

Depth is read from the file's columns rather than assumed: it differs by venue and
has changed across the archive. A level whose price is absent ends the book — and
absent has two spellings, because a column that is null for every row in a file
comes back as `None` where parquet typed it null, while one null for only some
rows comes back as `NaN` in a float column.

## Dates are JST

Every `date=` partition is an Asia/Tokyo day spanning 15:00-14:59 UTC, and the
`ts` inside the files is a UTC epoch. `DtRange.days` produces the same ISO date
strings the partitions use, so a range maps onto files with no conversion.
"""

from abc import ABC, abstractmethod
from logging import getLogger
from pathlib import Path

import komachi
import pandas as pd
from hase import layout, store
from hase.store import MissingDataError, book_depth
from qate.core.ev_type import EventType
from qate.core.model import Market, OrderBook, OrderLevel, Side, TimeSeriesData, Trade

LOG = getLogger(__name__)

TRADE = "Trade"
ORDER_BOOK = "OrderBook"
DATASETS = (TRADE, ORDER_BOOK)

Event = tuple[str, TimeSeriesData]


class MarketDataNotFound(Exception):
    pass


def data_root(override: str | Path | None = None) -> Path:
    """An explicit override, else wherever komachi says the data is.

    Asking komachi rather than deriving it means a backtest reads exactly where
    the downloader wrote, and keeps working when that setting changes. `env=True`
    is komachi's rule for a command line: `ROOT_PATH` in the environment is a
    per-run answer somebody typed on purpose, and wins over the settings file.
    """
    if override:
        return Path(override).expanduser()
    return Path(komachi.data_root(env=True))


def available_dates(root: str | Path, dataset: str, market: Market) -> list[str]:
    """Which days of a dataset are complete on disk."""
    return layout.available_dates(Path(root), market.id, dataset)


def available_markets(root: str | Path) -> list[Market]:
    """Every market komachi reports on disk that this install can replay.

    A market komachi holds but `qate` has no `ExchangeName` or `Symbol` for is
    skipped rather than raised on: it is real data, readable by other tools, and
    simply not something this install can turn into events.
    """
    markets = []
    for market_id in komachi.bronze.markets(Path(root)):
        try:
            markets.append(Market.from_str(market_id))
        except KeyError:
            LOG.info(f"Skipping {market_id}: not a market this install knows")
    return markets


class BronzeReader(ABC):
    """One market and dataset, read a day at a time.

    `load` returns a day's events as `(EventType, object)` pairs in file order,
    which for bronze is timestamp order. A day that is not on disk reads as empty
    rather than raising, so a gap in the archive costs that day and not the run;
    `available_dates` is how a caller checks first.
    """

    dataset: str

    def __init__(self, root: str | Path, market: Market):
        self.root = Path(root)
        self.market = market

    def available_dates(self) -> list[str]:
        return available_dates(self.root, self.dataset, self.market)

    def read_a_day(self, date_str: str) -> pd.DataFrame:
        try:
            df = self.read(date_str)
        except MissingDataError:
            LOG.warning(f"No {self.dataset} data for {self.market.id} on {date_str}")
            return pd.DataFrame()
        LOG.info(f"Loaded {len(df)} {self.dataset} rows for {self.market.id} {date_str}")
        return df

    @abstractmethod
    def read(self, date_str: str) -> pd.DataFrame:
        """hase's frame for one day. Raises `MissingDataError` if it is not on disk."""

    def load(self, date_str: str) -> list[Event]:
        df = self.read_a_day(date_str)
        if df.empty:
            return []
        return self.convert(df)

    def load_days(self, days: list[str]) -> list[Event]:
        events: list[Event] = []
        for date_str in days:
            events.extend(self.load(date_str))
        return events

    @abstractmethod
    def convert(self, df: pd.DataFrame) -> list[Event]:
        pass


class TradeReader(BronzeReader):
    dataset = TRADE

    def read(self, date_str: str) -> pd.DataFrame:
        return store.load_trades(self.root, self.market.id, date_str)

    def convert(self, df: pd.DataFrame) -> list[Event]:
        symbol = self.market.symbol
        exchange_name = self.market.exchange_name
        return [
            (
                EventType.MARKET_TRADE,
                Trade(
                    "",
                    Side.BUY if row.side == 0 else Side.SELL,
                    row.price,
                    row.size,
                    symbol,
                    exchange_name,
                    row.ts,
                ),
            )
            for row in df.itertuples(index=False)
        ]


class OrderBookReader(BronzeReader):
    dataset = ORDER_BOOK

    def read(self, date_str: str) -> pd.DataFrame:
        return store.load_order_book(self.root, self.market.id, date_str, depth=None)

    def convert(self, df: pd.DataFrame) -> list[Event]:
        depth = book_depth(df.columns)
        if depth == 0:
            raise MarketDataNotFound(
                f"{self.dataset} file for {self.market.id} has no bid/ask level columns: {list(df.columns)[:8]}"
            )

        symbol = self.market.symbol
        exchange_name = self.market.exchange_name

        # Read by position, not by name: itertuples over 80+ named columns is
        # measurably slower, and a full-depth day is millions of rows.
        ordered = ["ts"]
        for side in ("bid", "ask"):
            for i in range(depth):
                ordered += [f"{side}{i}_price", f"{side}{i}_qty"]
        df = df[ordered]

        ask_start = 1 + 2 * depth
        events = []
        for row in df.itertuples(index=False, name=None):
            events.append(
                (
                    EventType.MARKET_ORDER_BOOK,
                    OrderBook(
                        _levels(row, 1, depth),
                        _levels(row, ask_start, depth),
                        symbol,
                        exchange_name,
                        row[0],
                    ),
                )
            )
        return events


def _levels(row: tuple, start: int, depth: int) -> list[OrderLevel]:
    """Price/qty pairs from `start`, stopping at the first absent level.

    Absent levels are only ever a tail: a book with four levels fills 0-3 and
    leaves the rest null. Stopping is right — levels below the touch are not
    optional, and a book with a hole in it is not a book.

    Both spellings of absent are handled. `price != price` is the NaN test,
    cheaper than `math.isnan` in a loop this hot, and it does not catch `None` —
    which is what a wholly-null column reads back as.
    """
    levels = []
    for i in range(depth):
        price = row[start + 2 * i]
        if price is None or price != price:
            break
        levels.append(OrderLevel(price, row[start + 2 * i + 1]))
    return levels


class BronzeStore:
    """The bronze data under one root, and readers onto it."""

    def __init__(self, root: str | Path | None = None):
        self.root = data_root(root)

    def create_trade(self, market: Market) -> TradeReader:
        return TradeReader(self.root, market)

    def create_order_book(self, market: Market) -> OrderBookReader:
        return OrderBookReader(self.root, market)

    def create_reader(self, market: Market, event_type: str) -> BronzeReader:
        """A reader for the dataset behind a market-data event type."""
        if event_type == EventType.MARKET_TRADE:
            return self.create_trade(market)
        if event_type == EventType.MARKET_ORDER_BOOK:
            return self.create_order_book(market)
        raise ValueError(f"No bronze dataset for event type {event_type}")

    def available_dates(self, market: Market, dataset: str) -> list[str]:
        return available_dates(self.root, dataset, market)

    def markets(self) -> list[Market]:
        return available_markets(self.root)
