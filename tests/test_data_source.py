"""Chunked replay: merge order, trimming, and the shared buffer's synchronisation.

The shared buffer is the part worth testing hardest. It is the only concurrency
in Enoshima, and the failure it can produce — one backtest loading a chunk while
another is still reading the previous one — would show up as wrong numbers rather
than as a crash.
"""

import threading

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hase.bronze import DataType
from hase.layout import data_path
from qate.core.ev_type import EventType
from qate.core.model import ExchangeName, Market
from qate.core.symbol import Symbol
from qate.util.dt_range import DtRange

from enoshima.data_source import DataSource, Loader, SharedBuffer, Stream, market_data_event_types, merge

MARKET = Market(ExchangeName.COINCHECK, Symbol.BTC_SPOT)
OTHER = Market(ExchangeName.BITBANK, Symbol.BTC_SPOT)


class FakeEvent:
    """Stands in for a TimeSeriesData: the merge only ever asks for the ts."""

    def __init__(self, ts: float, label: str = ""):
        self.ts = ts
        self.label = label

    def get_ts(self) -> float:
        return self.ts


def ev(ts: float, label: str = "") -> tuple[str, FakeEvent]:
    return (EventType.MARKET_TRADE, FakeEvent(ts, label))


def day_start(date_str: str) -> float:
    compact = date_str.replace("-", "")
    return DtRange.from_strings(compact, compact).start_ts


def write_trades(root, market, date_str, timestamps):
    path = data_path(root, market.id, DataType.TRADE, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [{"ts": ts, "side": 0, "price": 100.0 + i, "size": 0.01} for i, ts in enumerate(timestamps)],
        columns=["ts", "side", "price", "size"],
    )
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)


# ----------------------------------------------------------------------- merge


def test_merge_interleaves_by_timestamp():
    a = [ev(1.0, "a"), ev(3.0, "a"), ev(5.0, "a")]
    b = [ev(2.0, "b"), ev(4.0, "b")]
    merged = merge([a, b])
    assert [e[1].ts for e in merged] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [e[1].label for e in merged] == ["a", "b", "a", "b", "a"]


def test_merge_of_one_stream_returns_it_unchanged():
    a = [ev(1.0), ev(2.0)]
    assert merge([a]) == a


def test_merge_ignores_empty_streams():
    a = [ev(1.0), ev(2.0)]
    assert [e[1].ts for e in merge([[], a, []])] == [1.0, 2.0]
    assert merge([]) == []
    assert merge([[], []]) == []


def test_merge_keeps_every_event_from_three_streams():
    streams = [[ev(float(i)) for i in range(start, 30, 3)] for start in range(3)]
    merged = merge(streams)
    assert [e[1].ts for e in merged] == [float(i) for i in range(30)]


def test_merge_handles_a_timestamp_shared_by_two_streams():
    """Books and trades routinely carry the same second; neither may be dropped."""
    merged = merge([[ev(1.0, "a"), ev(1.0, "a")], [ev(1.0, "b")]])
    assert len(merged) == 3
    assert [e[1].ts for e in merged] == [1.0, 1.0, 1.0]


# ---------------------------------------------------------------------- loader


def test_loader_reads_the_range_in_chunks(tmp_path):
    for date_str in ("2026-01-01", "2026-01-02", "2026-01-03"):
        write_trades(tmp_path, MARKET, date_str, [day_start(date_str) + 1])

    loader = Loader(DtRange.from_strings("20260101", "20260103"), chunk_days=2)
    loader.add_stream(Stream(tmp_path, MARKET, EventType.MARKET_TRADE))

    first = loader.load_more()
    assert len(first) == 2  # 01-01 and 01-02
    second = loader.load_more()
    assert len(second) == 1  # 01-03
    assert loader.load_more() is None


def test_loader_reset_replays_from_the_start(tmp_path):
    write_trades(tmp_path, MARKET, "2026-01-01", [day_start("2026-01-01") + 1])
    loader = Loader(DtRange.from_strings("20260101", "20260101"), chunk_days=1)
    loader.add_stream(Stream(tmp_path, MARKET, EventType.MARKET_TRADE))

    assert len(loader.load_more()) == 1
    assert loader.load_more() is None
    loader.reset()
    assert len(loader.load_more()) == 1


def test_loader_trims_to_the_exact_range(tmp_path):
    """A range starting mid-day must not replay that day's earlier events."""
    start = day_start("2026-01-01")
    write_trades(tmp_path, MARKET, "2026-01-01", [start + 60, start + 3600 + 60, start + 7200 + 60])

    loader = Loader(DtRange.from_strings("20260101_0030", "20260101_0130"), chunk_days=1)
    loader.add_stream(Stream(tmp_path, MARKET, EventType.MARKET_TRADE))

    events = loader.load_more()
    assert [e[1].get_ts() for e in events] == [start + 3600 + 60]


def test_loader_merges_two_markets(tmp_path):
    start = day_start("2026-01-01")
    write_trades(tmp_path, MARKET, "2026-01-01", [start + 1, start + 3])
    write_trades(tmp_path, OTHER, "2026-01-01", [start + 2, start + 4])

    loader = Loader(DtRange.from_strings("20260101", "20260101"), chunk_days=1)
    loader.add_stream(Stream(tmp_path, MARKET, EventType.MARKET_TRADE))
    loader.add_stream(Stream(tmp_path, OTHER, EventType.MARKET_TRADE))

    events = loader.load_more()
    assert [e[1].get_ts() for e in events] == [start + 1, start + 2, start + 3, start + 4]


def test_a_missing_day_costs_that_day_not_the_run(tmp_path):
    write_trades(tmp_path, MARKET, "2026-01-01", [day_start("2026-01-01") + 1])
    loader = Loader(DtRange.from_strings("20260101", "20260102"), chunk_days=2)
    loader.add_stream(Stream(tmp_path, MARKET, EventType.MARKET_TRADE))

    assert len(loader.load_more()) == 1


# --------------------------------------------------------------- shared buffer


class FakeLoader:
    """Hands out fixed chunks and records how many times it was asked."""

    def __init__(self, chunks: list[list]):
        self.chunks = list(chunks)
        self.calls = 0

    def load_more(self):
        self.calls += 1
        return self.chunks.pop(0) if self.chunks else None

    def reset(self):
        pass


def test_shared_buffer_loads_once_per_chunk_for_many_readers():
    """Four channels over two chunks must cause two loads, not eight."""
    loader = FakeLoader([[ev(1.0)], [ev(2.0)]])
    buffer = SharedBuffer(loader)
    for _ in range(4):
        buffer.increment_ref()

    drained = []

    def drain():
        pos = 0
        while True:
            if pos == buffer.len:
                if not buffer.request_load_more():
                    return
                pos = 0
            drained.append(buffer.get(pos)[1].ts)
            pos += 1

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a reader never finished; the buffer deadlocked"

    # Two real chunks plus the None that ends it.
    assert loader.calls == 3
    # Every reader saw both events.
    assert sorted(drained) == [1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0]


def test_shared_buffer_reports_exhaustion():
    loader = FakeLoader([])
    buffer = SharedBuffer(loader)
    buffer.increment_ref()
    assert buffer.request_load_more() is False


# ------------------------------------------------------------------ datasource


def test_channel_puts_trading_events_before_market_events(tmp_path):
    """An order response must reach the strategy before the next book does."""
    start = day_start("2026-01-01")
    write_trades(tmp_path, MARKET, "2026-01-01", [start + 1, start + 2])

    ds = DataSource(DtRange.from_strings("20260101", "20260101"), tmp_path, chunk_days=1)
    ds.add_source(MARKET, [EventType.MARKET_TRADE])
    channel = ds.create_channel()

    first = channel.get()
    assert first[1].get_ts() == start + 1

    channel.put(("ORDER_FILLED", FakeEvent(0.0, "fill")))
    assert channel.get()[0] == "ORDER_FILLED"
    assert channel.get()[1].get_ts() == start + 2
    assert channel.get() is None


def test_check_coverage_names_the_missing_days(tmp_path):
    write_trades(tmp_path, MARKET, "2026-01-01", [day_start("2026-01-01") + 1])
    ds = DataSource(DtRange.from_strings("20260101", "20260103"), tmp_path)
    missing = ds.check_coverage([(MARKET, [EventType.MARKET_TRADE])])
    assert missing == [
        "COINCHECK:BTC_SPOT Trade 2026-01-02",
        "COINCHECK:BTC_SPOT Trade 2026-01-03",
    ]


def test_check_coverage_is_empty_when_everything_is_there(tmp_path):
    for date_str in ("2026-01-01", "2026-01-02"):
        write_trades(tmp_path, MARKET, date_str, [day_start(date_str) + 1])
    ds = DataSource(DtRange.from_strings("20260101", "20260102"), tmp_path)
    assert ds.check_coverage([(MARKET, [EventType.MARKET_TRADE])]) == []


@pytest.mark.parametrize(
    "given,expected",
    [
        ([EventType.MARKET_TRADE, EventType.MARKET_BAR], [EventType.MARKET_TRADE]),
        ([EventType.MARKET_ORDER_BOOK], [EventType.MARKET_ORDER_BOOK]),
        ([EventType.ORDER_FILLED], []),
    ],
)
def test_only_event_types_bronze_can_supply_are_replayed(given, expected):
    assert market_data_event_types(given) == expected


def test_reset_allows_a_second_batch_over_the_same_range(tmp_path):
    start = day_start("2026-01-01")
    write_trades(tmp_path, MARKET, "2026-01-01", [start + 1, start + 2])
    ds = DataSource(DtRange.from_strings("20260101", "20260101"), tmp_path, chunk_days=1)
    ds.add_source(MARKET, [EventType.MARKET_TRADE])

    def drain_one_channel():
        channel = ds.create_channel()
        count = 0
        while channel.get() is not None:
            count += 1
        return count

    assert drain_one_channel() == 2
    ds.reset()
    assert drain_one_channel() == 2


def test_order_book_and_trade_streams_are_both_added(tmp_path):
    ds = DataSource(DtRange.from_strings("20260101", "20260101"), tmp_path)
    ds.add_source(MARKET, [EventType.MARKET_ORDER_BOOK, EventType.MARKET_TRADE])
    datasets = {s.data_type for s in ds._loader._streams}
    assert datasets == {DataType.ORDER_BOOK, DataType.TRADE}


def test_events_are_tagged_with_their_type(tmp_path):
    write_trades(tmp_path, MARKET, "2026-01-01", [day_start("2026-01-01") + 1])
    [(event_type, trade)] = Stream(tmp_path, MARKET, EventType.MARKET_TRADE).load("2026-01-01")
    assert event_type == EventType.MARKET_TRADE
    assert trade.market == MARKET


def test_a_stream_refuses_an_event_type_bronze_cannot_supply(tmp_path):
    with pytest.raises(ValueError):
        Stream(tmp_path, MARKET, EventType.MARKET_BAR)
