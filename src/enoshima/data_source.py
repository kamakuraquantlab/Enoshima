"""Feeding recorded market data to many backtests at once, without running out of memory.

A backtest is bounded by memory, not by CPU. A single day of full-depth order
books for one market is a few hundred megabytes once it is Python objects, and an
optimize run wants the same days replayed against dozens of parameter sets. Load
the whole range once per backtest and a week of books will exhaust any machine;
load it once and share it, and the run is bounded by one chunk.

That is what this module is: the range is loaded a few days at a time into one
buffer, every backtest reads from that same buffer, and the next chunk is not
loaded until all of them have finished the current one.

```
BronzeStore ──► Loader ──► SharedBuffer ──► Channel ──► Backtest
  (parquet)     chunks     one copy         one per     one strategy,
                of days    of the events    backtest    one param set
```

Reading is `qate.data.bronze`'s job; this module decides *when*, and hands out
one `Channel` per backtest. `Channel` is the chunked counterpart of
`qate.simulator.ReplayQueue`: the same "the strategy's own events come first"
rule, over a buffer that is refilled rather than a fixed list.
"""

import threading
import time
from logging import getLogger

from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.model import Market, TimeSeriesData
from qate.data.bronze import BronzeReader, BronzeStore
from qate.util.dt_range import DtRange

LOG = getLogger(__name__)

Event = tuple[str, TimeSeriesData]

# Days loaded per chunk. Low on purpose: full-depth order books are heavy, and a
# seven-day chunk of 20-level books has been measured at over 100GB resident.
# Raising this trades memory for fewer parquet reads.
DEFAULT_CHUNK_DAYS = 2


class Series:
    """A cursor into one loaded stream, for the merge."""

    def __init__(self, data: list[Event]):
        self.data = data
        self.pos = 0

    @property
    def no_more_data(self) -> bool:
        return self.pos == len(self.data)

    @property
    def top_ts(self) -> float:
        return self.data[self.pos][1].get_ts()

    def get(self) -> Event:
        item = self.data[self.pos]
        self.pos += 1
        return item


def merge(data_list: list[list[Event]]) -> list[Event]:
    """Interleave already-sorted streams into one timestamp-ordered list.

    A linear scan across cursors rather than `heapq.merge`, because the number of
    streams is small — a book and a trade feed, sometimes a second market — and
    the list is materialised anyway for the shared buffer.
    """
    series_list = [Series(x) for x in data_list if x]
    if not series_list:
        return []
    if len(series_list) == 1:
        return series_list[0].data

    buffer: list[Event] = []
    while True:
        min_ts = None
        min_series = None

        for series in series_list:
            if series.no_more_data:
                continue
            if min_ts is None or series.top_ts < min_ts:
                min_ts = series.top_ts
                min_series = series

        if min_series is None:
            break
        buffer.append(min_series.get())

    return buffer


class Loader:
    """Reads the range a chunk of days at a time, merged across every stream."""

    def __init__(self, dt_range: DtRange, chunk_days: int = DEFAULT_CHUNK_DAYS):
        self._dt_range = dt_range
        self._days = dt_range.days
        self._chunk_days = chunk_days
        self._readers: list[BronzeReader] = []
        self.reset()

    def add_reader(self, reader: BronzeReader) -> None:
        self._readers.append(reader)

    def _trim(self, data: list[Event]) -> list[Event]:
        """Drop events outside the exact range.

        Day partitions get us to the right files; the range may start and end
        mid-day. Skipped entirely when the whole chunk is already inside it,
        which is the common case for every chunk but the first and last.
        """
        if not data:
            return data
        if self._dt_range.is_within(data[0][1].get_ts()) and self._dt_range.is_within(data[-1][1].get_ts()):
            return data

        start = 0
        while start < len(data) and data[start][1].get_ts() < self._dt_range.start_ts:
            start += 1

        end = len(data)
        while end > start and data[end - 1][1].get_ts() > self._dt_range.end_ts:
            end -= 1
        return data[start:end]

    def load_more(self) -> list[Event] | None:
        """The next chunk, or None when the range is exhausted."""
        if self._pos >= len(self._days):
            return None

        days = self._days[self._pos : self._pos + self._chunk_days]
        self._pos += self._chunk_days

        # One series per reader, then merged: a strategy may subscribe to books
        # and trades from more than one market, and all of them have to arrive in
        # timestamp order.
        series_list = [self._trim(reader.load_days(days)) for reader in self._readers]
        events = merge(series_list)
        LOG.info(f"Loaded {days[0]}..{days[-1]}: {len(events)} events")
        return events

    def reset(self) -> None:
        self._pos = 0


class SharedBuffer:
    """One chunk of events, shared by every backtest in a batch.

    The synchronisation is the point. Each `Channel` calls `request_load_more`
    when it reaches the end of the buffer and blocks; the last one to arrive does
    the load and wakes the rest. So the buffer is only ever replaced when nobody
    is reading it, and only one copy of a chunk exists no matter how many
    parameter sets are running.

    A backtest that raises rather than draining its channel would leave the
    others waiting. `Runner` waits on the futures and surfaces the exception, so
    the run fails rather than hanging silently.
    """

    def __init__(self, loader: Loader):
        self._loader = loader
        self._buffer: list[Event] | None = []
        self._num_refs = 0
        self._num_waiting = 0
        self._cond = threading.Condition()

    def increment_ref(self) -> None:
        self._num_refs += 1

    def get(self, pos: int) -> Event:
        return self._buffer[pos]

    @property
    def len(self) -> int:
        return len(self._buffer) if self._buffer is not None else 0

    def request_load_more(self) -> bool:
        """Block until the next chunk is loaded. False when there is none."""
        with self._cond:
            self._num_waiting += 1
            if self._num_waiting == self._num_refs:
                self._buffer = self._loader.load_more()
                self._num_waiting = 0
                self._cond.notify_all()
            else:
                self._cond.wait()
        return self._buffer is not None

    def reset(self) -> None:
        self._num_refs = 0
        self._num_waiting = 0
        self._buffer = []


class Channel(EventQueue):
    """One backtest's view of the shared buffer, plus its own trading events.

    Order matters and is the same rule `qate.simulator.ReplayQueue` documents: an
    order response has to reach the strategy before the next market event does,
    or the strategy is shown a new book while still believing an order is open.
    """

    def __init__(self, shared_buffer: SharedBuffer):
        self._shared_buffer = shared_buffer
        self._trading_events: list[Event] = []
        self._pos = 0
        self._done = False

    def put(self, event: Event) -> None:
        self._trading_events.append(event)

    def get(self, timeout=0) -> Event | None:
        if self._trading_events:
            return self._trading_events.pop(0)
        return self._next_market_event()

    def _next_market_event(self) -> Event | None:
        if self._done:
            return None
        if self._pos == self._shared_buffer.len:
            if not self._shared_buffer.request_load_more():
                self._done = True
                return None
            self._pos = 0
        item = self._shared_buffer.get(self._pos)
        self._pos += 1
        return item


class DataSource:
    """The bronze data a run replays, and the channels onto it."""

    def __init__(
        self,
        dt_range: DtRange,
        store: BronzeStore,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
    ):
        self.store = store
        self.dt_range = dt_range
        self._loader = Loader(dt_range, chunk_days)
        self._shared_buffer = SharedBuffer(self._loader)

    def add_source(self, market: Market, event_types: list[str]) -> None:
        for event_type in event_types:
            self._loader.add_reader(self.store.create_reader(market, event_type))

    def create_channel(self) -> Channel:
        self._shared_buffer.increment_ref()
        return Channel(self._shared_buffer)

    def reset(self) -> None:
        """Rewind for the next batch of parameter sets."""
        self._loader.reset()
        self._shared_buffer.reset()

    def check_coverage(self, markets: list[tuple[Market, list[str]]]) -> list[str]:
        """Which days the range asks for that are not on disk.

        Reported before a run rather than discovered during one. A gap is not
        fatal — a missing day reads as empty — but a result computed over data
        that is half absent is worth knowing about before it is acted on.
        """
        wanted = set(self.dt_range.days)
        missing: set[str] = set()
        for market, event_types in markets:
            for event_type in event_types:
                reader = self.store.create_reader(market, event_type)
                have = set(reader.available_dates())
                for day in sorted(wanted - have):
                    missing.add(f"{market.id} {reader.dataset} {day}")
        return sorted(missing)


def market_data_event_types(event_types: list[str]) -> list[str]:
    """Only the event types bronze can supply."""
    return [x for x in event_types if x in (EventType.MARKET_ORDER_BOOK, EventType.MARKET_TRADE)]


if __name__ == "__main__":
    # Read one market's trades straight through, to check a data root and see
    # what the throughput is: python -m enoshima.data_source COINCHECK:BTC_SPOT
    import logging
    import sys

    logging.basicConfig(level=logging.INFO, format="{asctime} {message}", style="{")
    market = Market.from_str(sys.argv[1] if len(sys.argv) > 1 else "COINCHECK:BTC_SPOT")
    started = time.time()

    ds = DataSource(DtRange.from_strings("2026-01-03", "2026-01-04"), BronzeStore())
    ds.add_source(market, [EventType.MARKET_TRADE])
    channel = ds.create_channel()
    count = 0
    while channel.get() is not None:
        count += 1
    print(f"{count} events in {time.time() - started:.1f}s")
