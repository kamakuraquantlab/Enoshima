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
hase.bronze ──► Loader ──► SharedBuffer ──► Channel ──► Backtest
 (Trade,         chunks     one copy         one per     one strategy,
  OrderBook)     of days    of the events    backtest    one param set
```

Reading is `hase.bronze`'s job: it is asked for a market-day and hands back
qate's `Trade`s and `OrderBook`s. Nothing here opens a file, knows a path, or
asks komachi anything. This module decides *when* to read, tags what comes back
with its event type, and hands out one `Channel` per backtest. `Channel` is the chunked counterpart of
`qate.trading.replay.ReplayQueue`: the same "the strategy's own events come first"
rule, over a buffer that is refilled rather than a fixed list.
"""

import threading
from logging import getLogger

from hase import bronze
from hase.bronze import DataType, MissingDataError
from qate.core.ev_q import EventQueue
from qate.core.ev_type import EventType
from qate.core.model import Market, TimeSeriesData
from qate.util.dt_range import DtRange

LOG = getLogger(__name__)

Event = tuple[str, TimeSeriesData]

# The market-data events bronze can supply, and the dataset behind each.
DATA_TYPES = {
    EventType.MARKET_TRADE: DataType.TRADE,
    EventType.MARKET_ORDER_BOOK: DataType.ORDER_BOOK,
}

# Days loaded per chunk. Low on purpose: full-depth order books are heavy, and a
# seven-day chunk of 20-level books has been measured at over 100GB resident.
# Raising this trades memory for fewer parquet reads.
DEFAULT_CHUNK_DAYS = 2


class Stream:
    """One market's events of one type, read a day at a time.

    A day that is not on disk reads as empty rather than raising, so a gap in
    the archive costs that day and not the run; `DataSource.check_coverage` is
    how a run says so before it starts.
    """

    def __init__(self, market: Market, event_type: str):
        if event_type not in DATA_TYPES:
            raise ValueError(f"No bronze dataset for event type {event_type}")
        self.market = market
        self.event_type = event_type
        self.data_type = DATA_TYPES[event_type]

    def available_dates(self) -> list[str]:
        return bronze.dates(self.market, self.data_type)

    def load(self, day: str) -> list[Event]:
        try:
            data = bronze.load_day(self.market, self.data_type, day)
        except MissingDataError:
            LOG.warning(f"No {self.data_type} data for {self.market.id} on {day}")
            return []
        LOG.info(f"Loaded {len(data)} {self.data_type} rows for {self.market.id} {day}")
        return [(self.event_type, x) for x in data]

    def load_days(self, days: list[str]) -> list[Event]:
        events: list[Event] = []
        for day in days:
            events.extend(self.load(day))
        return events


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
        self._streams: list[Stream] = []
        self.reset()

    def add_stream(self, stream: Stream) -> None:
        self._streams.append(stream)

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

        # One series per stream, then merged: a strategy may subscribe to books
        # and trades from more than one market, and all of them have to arrive in
        # timestamp order.
        series_list = [self._trim(stream.load_days(days)) for stream in self._streams]
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

    Order matters and is the same rule `qate.trading.replay.ReplayQueue` documents: an
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

    def __init__(self, dt_range: DtRange, chunk_days: int = DEFAULT_CHUNK_DAYS):
        self.dt_range = dt_range
        self._loader = Loader(dt_range, chunk_days)
        self._shared_buffer = SharedBuffer(self._loader)

    def add_source(self, market: Market, event_types: list[str]) -> None:
        for event_type in event_types:
            self._loader.add_stream(Stream(market, event_type))

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
                stream = Stream(market, event_type)
                have = set(stream.available_dates())
                for day in sorted(wanted - have):
                    missing.add(f"{market.id} {stream.data_type} {day}")
        return sorted(missing)


def market_data_event_types(event_types: list[str]) -> list[str]:
    """Only the event types bronze can supply."""
    return [x for x in event_types if x in DATA_TYPES]
