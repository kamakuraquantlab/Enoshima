"""Reading the bronze layer: paths, schemas, and the ragged edges of a book.

The fixtures are written with pyarrow directly rather than through
`qate.store.timeseries`, so a change to the writer cannot quietly redefine what
the reader is being tested against.

Paths and the list of complete days come from `komachi`, so the tests that cover
them are really checking that this module asks it correctly and converts between
the two vocabularies — komachi's `"EX:SYM"` strings and qate's `Market`.
"""

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market, Side
from qate.core.symbol import Symbol

from enoshima import bronze

MARKET = Market(ExchangeName.COINCHECK, Symbol.BTC_SPOT)
DATE = "2026-01-15"


def write_parquet(root, dataset, market, date_str, rows: list[dict], columns: list[str]):
    path = bronze.data_path(root, dataset, market, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=columns)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
    return path


def book_columns(depth: int) -> list[str]:
    cols = ["ts"]
    for side in ("bid", "ask"):
        for i in range(depth):
            cols += [f"{side}{i}_price", f"{side}{i}_qty"]
    return cols


def book_row(ts: float, bids: list[tuple], asks: list[tuple], depth: int) -> dict:
    row = {"ts": ts}
    for side, levels in (("bid", bids), ("ask", asks)):
        for i in range(depth):
            price, qty = levels[i] if i < len(levels) else (None, None)
            row[f"{side}{i}_price"] = price
            row[f"{side}{i}_qty"] = qty
    return row


def test_data_path_is_the_hive_layout(tmp_path):
    path = bronze.data_path(tmp_path, bronze.TRADE, MARKET, DATE)
    assert path == (
        tmp_path
        / "bronze"
        / "dataset=Trade"
        / "exchange=COINCHECK"
        / "symbol=BTC_SPOT"
        / f"date={DATE}"
        / "data.parquet"
    )


def test_trades_are_read_in_file_order(tmp_path):
    write_parquet(
        tmp_path,
        bronze.TRADE,
        MARKET,
        DATE,
        [
            {"ts": 100.0, "side": 0, "price": 15_000_000.0, "size": 0.01},
            {"ts": 100.5, "side": 1, "price": 15_000_100.0, "size": 0.02},
        ],
        ["ts", "side", "price", "size"],
    )

    events = bronze.TradeReader(tmp_path, MARKET).load(DATE)
    assert [e[0] for e in events] == [EventType.MARKET_TRADE] * 2

    first, second = events[0][1], events[1][1]
    assert (first.side, first.price, first.size, first.get_ts()) == (Side.BUY, 15_000_000.0, 0.01, 100.0)
    assert second.side == Side.SELL
    assert first.market == MARKET


def test_order_books_are_read_with_depth_from_the_columns(tmp_path):
    depth = 3
    write_parquet(
        tmp_path,
        bronze.ORDER_BOOK,
        MARKET,
        DATE,
        [book_row(100.0, [(99.0, 1.0), (98.0, 2.0), (97.0, 3.0)], [(101.0, 1.5), (102.0, 2.5), (103.0, 3.5)], depth)],
        book_columns(depth),
    )

    events = bronze.OrderBookReader(tmp_path, MARKET).load(DATE)
    assert len(events) == 1
    event_type, book = events[0]
    assert event_type == EventType.MARKET_ORDER_BOOK
    assert [level.price for level in book.bids] == [99.0, 98.0, 97.0]
    assert [level.amount for level in book.asks] == [1.5, 2.5, 3.5]
    assert book.best_bid == 99.0
    assert book.best_ask == 101.0
    assert book.get_ts() == 100.0


def test_a_short_book_stops_at_the_absent_level(tmp_path):
    """A venue that showed two levels must not read back as twenty."""
    depth = 20
    write_parquet(
        tmp_path,
        bronze.ORDER_BOOK,
        MARKET,
        DATE,
        [book_row(100.0, [(99.0, 1.0), (98.0, 2.0)], [(101.0, 1.5)], depth)],
        book_columns(depth),
    )

    _, book = bronze.OrderBookReader(tmp_path, MARKET).load(DATE)[0]
    assert len(book.bids) == 2
    assert len(book.asks) == 1


def test_book_depth_reads_the_columns():
    assert bronze.book_depth(book_columns(20)) == 20
    assert bronze.book_depth(book_columns(1)) == 1
    assert bronze.book_depth(["ts", "side", "price", "size"]) == 0


def test_a_book_file_with_no_levels_is_an_error(tmp_path):
    write_parquet(tmp_path, bronze.ORDER_BOOK, MARKET, DATE, [{"ts": 1.0}], ["ts"])
    with pytest.raises(bronze.MarketDataNotFound):
        bronze.OrderBookReader(tmp_path, MARKET).load(DATE)


def test_a_missing_day_reads_as_empty(tmp_path):
    """A gap in the archive costs that day, not the run."""
    assert bronze.TradeReader(tmp_path, MARKET).load("2026-01-14") == []


def test_available_dates_ignores_a_partial_download(tmp_path):
    write_parquet(tmp_path, bronze.TRADE, MARKET, "2026-01-15", [{"ts": 1.0, "side": 0, "price": 1.0, "size": 1.0}], ["ts", "side", "price", "size"])
    # A date directory with no data.parquet: interrupted mid-download.
    bronze.data_path(tmp_path, bronze.TRADE, MARKET, "2026-01-16").parent.mkdir(parents=True)

    reader = bronze.TradeReader(tmp_path, MARKET)
    assert reader.available_dates() == ["2026-01-15"]


def test_store_lists_markets_and_skips_unknown_ones(tmp_path):
    cols = ["ts", "side", "price", "size"]
    rows = [{"ts": 1.0, "side": 0, "price": 1.0, "size": 1.0}]
    write_parquet(tmp_path, bronze.TRADE, MARKET, DATE, rows, cols)

    # A market on disk that this install has no ExchangeName for: readable by
    # other tools, not replayable here, and not a crash.
    unknown = tmp_path / "bronze" / "dataset=Trade" / "exchange=SOMEVENUE" / "symbol=BTC_SPOT" / f"date={DATE}"
    unknown.mkdir(parents=True)
    (unknown / "data.parquet").write_bytes(b"")

    store = bronze.BronzeStore(tmp_path)
    assert store.markets() == [MARKET]
    assert store.available_dates(MARKET, bronze.TRADE) == [DATE]


def test_create_reader_maps_event_types(tmp_path):
    store = bronze.BronzeStore(tmp_path)
    assert isinstance(store.create_reader(MARKET, EventType.MARKET_TRADE), bronze.TradeReader)
    assert isinstance(store.create_reader(MARKET, EventType.MARKET_ORDER_BOOK), bronze.OrderBookReader)
    with pytest.raises(ValueError):
        store.create_reader(MARKET, EventType.MARKET_BAR)


def test_explicit_root_wins_over_komachi(tmp_path):
    assert bronze.data_root(tmp_path) == tmp_path
