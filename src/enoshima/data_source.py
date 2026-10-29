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
with its event type, and hands out one `Channel` per backtest. `Channel` is the
`qate.core.ev_q.EventQueue` a backtest runs on: the strategy's own events come
first, then the shared buffer, which is refilled chunk by chunk.
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
    when it reaches the end of the buffer and blocks; when the last one arrives
    the buffer is replaced and everyone is woken. So the buffer is only ever
    replaced when nobody is reading it, and every backtest reads the same copy.

    The next chunk is loaded ahead, on one dedicated thread, while the
    backtests replay the current one: loading is single-threaded and was most
    of what every other thread spent waiting for. At most two chunks exist at
    once, the one being read and the one waiting to replace it.

    The thread is dedicated, not whichever backtest happened to arrive last,
    for memory as well as speed. On a free-threaded build each thread
    allocates from its own heap, and memory one thread allocates and another
    frees is not reliably reused: with a different backtest loading each
    chunk, a 45-way sweep grew by about a gigabyte per replayed day until it
    ran out. Loading and releasing every chunk on the same thread keeps it flat.

    A backtest that stops early -- it failed, or was stopped -- would leave
    the others waiting for it at the barrier forever, so its channel
    `release`s its place and the rest carry on without it. A load that raises
    is handed to every waiting channel, and `abort` wakes them all with no
    more data, for the same reason: nothing here may wait on a thread that is
    not coming.
    """

    def __init__(self, loader: Loader):
        self._loader = loader
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._init_state()

    def _init_state(self) -> None:
        self._buffer: list[Event] | None = []
        self._num_refs = 0
        self._num_waiting = 0
        # Bumped each time the buffer is replaced, so a waiter can tell its own
        # replacement from a spurious wake-up.
        self._generation = 0
        self._swap_requested = False
        self._stop = False
        self._aborted = False
        self._error: BaseException | None = None

    def increment_ref(self) -> None:
        self._num_refs += 1

    def get(self, pos: int) -> Event:
        return self._buffer[pos]

    @property
    def len(self) -> int:
        return len(self._buffer) if self._buffer is not None else 0

    def request_load_more(self) -> bool:
        """Block until the next chunk replaces this one. False when there is none."""
        with self._cond:
            if self._aborted:
                return False
            if self._thread is None:
                self._thread = threading.Thread(target=self._load_loop, name="chunk-loader", daemon=True)
                self._thread.start()
            generation = self._generation
            self._num_waiting += 1
            self._swap_if_all_arrived()
            while self._generation == generation and not self._aborted:
                self._cond.wait()
            if self._aborted:
                return False
            if self._error is not None:
                raise RuntimeError("loading the next chunk failed") from self._error
        return self._buffer is not None

    def release(self) -> None:
        """A channel leaves for good. The others no longer wait for it."""
        with self._cond:
            self._num_refs -= 1
            self._swap_if_all_arrived()

    def abort(self) -> None:
        """Wake every waiting channel with no more data, and load nothing more."""
        with self._cond:
            self._aborted = True
            self._stop = True
            self._cond.notify_all()

    def _swap_if_all_arrived(self) -> None:
        # Called with the lock held.
        if self._num_refs > 0 and self._num_waiting == self._num_refs:
            self._swap_requested = True
            self._cond.notify_all()

    def _load_loop(self) -> None:
        while True:
            # Load ahead: this runs while the backtests read the current chunk.
            try:
                data, error = self._loader.load_more(), None
            except BaseException as e:  # handed to the waiters, who raise it
                data, error = None, e
            with self._cond:
                while not self._swap_requested and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                self._swap_requested = False
                previous, self._buffer = self._buffer, data
                self._error = error
                self._num_waiting = 0
                self._generation += 1
                self._cond.notify_all()
            # Released here, on the thread that allocated it.
            del previous
            if data is None:
                return

    def reset(self) -> None:
        """Stop loading ahead, so the loader can be rewound for the next batch.

        A chunk already being loaded is waited for and dropped: it was read
        from where the previous batch had got to, not from the start.
        """
        with self._cond:
            self._stop = True
            self._cond.notify_all()
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join()
        self._init_state()


class Channel(EventQueue):
    """One backtest's view of the shared buffer, plus its own trading events.

    Order matters: an
    order response has to reach the strategy before the next market event does,
    or the strategy is shown a new book while still believing an order is open.
    """

    def __init__(self, shared_buffer: SharedBuffer):
        self._shared_buffer = shared_buffer
        self._trading_events: list[Event] = []
        self._pos = 0
        self._done = False
        self._closed = False

    def put(self, event: Event) -> None:
        self._trading_events.append(event)

    def get(self, timeout=0) -> Event | None:
        if self._trading_events:
            return self._trading_events.pop(0)
        return self._next_market_event()

    def _next_market_event(self) -> Event | None:
        if self._done:
            return None
        # A loop, not an if: a chunk can be empty -- a range of days with no
        # data on disk -- and reading index 0 of it raised in every backtest.
        while self._pos >= self._shared_buffer.len:
            if not self._shared_buffer.request_load_more():
                self._done = True
                return None
            self._pos = 0
        item = self._shared_buffer.get(self._pos)
        self._pos += 1
        return item

    def close(self) -> None:
        """This backtest reads no more. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if not self._done:
            self._shared_buffer.release()


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
        """Rewind for the next batch of parameter sets.

        The buffer first: it stops the thread that loads ahead, which would
        otherwise still be reading from where the last batch got to.
        """
        self._shared_buffer.reset()
        self._loader.reset()

    def abort(self) -> None:
        """End the replay for every channel now: the run is being stopped."""
        self._shared_buffer.abort()

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
