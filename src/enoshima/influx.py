"""Optional: record a backtest's metrics, orders and PnL in InfluxDB, for Grafana.

`InfluxReporter` is a `qate.trading.reporter.Reporter`, added to every run of an
environment whose `enoshima.json` has an `"influx"` entry. It writes as the run
goes, one bucket per parameter set, so a finished backtest is on a dashboard with
nothing further to do.

The live system gets there in three steps -- a msgpack log written in real time,
an upload, and a loader into InfluxDB -- because a trading process must never
wait on a database. A backtest has no such constraint and no reason for the
intermediate file, so it writes the destination directly.

It is optional because a backtest's *result* does not need it: without it
a run still evaluates itself and records its figures in the run log. The token
and URLs come from the `"influx"` entry itself.

## Timestamps

Metric records carry float epoch seconds. Points are written at microsecond
precision, and points of one series -- measurement, tags and field names -- that
share a timestamp are spread by 1us each, because InfluxDB treats series +
timestamp as a primary key and would otherwise overwrite rather than append. Two
fills inside the same millisecond are ordinary, so this is load-bearing rather
than defensive.

The spreading must also be the same in every run, so that re-running a parameter
set over the same range overwrites its points rather than doubling them. It was
not: ties were counted across every point in a write buffer, whatever its series,
and restarted at each flush, so a point's offset depended on its neighbours and
on where the buffer broke. A change elsewhere -- fewer metrics buffered -- moved
the offsets, and a re-run added a second copy of about 0.5% of a sweep's points.
`TimestampSpreader` counts per series and across flushes, so a point's offset
depends only on how many earlier points of its own series share its timestamp.

## One bucket per parameter set and batch

`backtest_<param_set_id>_<batch_id>`: the id naming the environment and the
parameters (see `param_id`), then the batch that ran it. A sweep's combinations
sit side by side under one batch id, and a dashboard switches between them with
a variable rather than a query rewrite. The run's `meta.json` records its bucket.

The batch id is in it so that every execution writes a bucket of its own. Without
it, a `--force` re-run wrote into the bucket of the run it repeated: points at the
same timestamps were replaced and the rest stayed, so after a strategy change one
bucket held two runs mixed together. A `--temporary` flag used to be the way to
ask for a fresh bucket; making every run fresh removed the need for it.

Every bucket is kept. Enoshima creates buckets and writes to them, and never
expires or deletes one: cleaning up is a separate tool's job. A retention on
temporary buckets used to make InfluxDB refuse any point older than 30 days,
which is every point of a backtest of last year.

## A weekly run's bucket: `backtest_<param_set_id>_w<run_id>`

A weekly run (`weekly`) is built up over several executions, and a window
reads weeks written by any of them, so it has one bucket for the run rather
than one per execution. Every point is tagged `week=<YYYY-Www>`, and a week's
points are deleted by that tag before the week is written again: by time
range would not do, because a position closed on a Monday has its open-aligned
copy in the Sunday before, in the previous week's time. The bucket then holds
exactly the weeks on disk, each once. This is the one delete Enoshima makes.
"""

import datetime as dt
from logging import getLogger

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import WriteOptions, WriteType
from qate.core.model import (
    DataType,
    ExchangeName,
    Field,
    Measurement,
    Metric,
    SettleType,
    Side,
    Tag,
)
from qate.core.order import OrderResponse, OrderType
from qate.core.symbol import Symbol
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.util.buffer import BufferedWriter
from qate.util.dt_range import DtRange

LOG = getLogger(__name__)


def in_range(ts: float, dt_range: DtRange) -> bool:
    return ts >= dt_range.start_ts

WRITE_TIMEOUT_MS = 120_000  # large batches from a finished backtest are slow


def create_client(url: str, token: str, org: str | None) -> InfluxDBClient:
    return InfluxDBClient(url, token, org=org, timeout=WRITE_TIMEOUT_MS)


class TimestampSpreader:
    """Moves points to microseconds and breaks ties within a series, the same way every run.

    One per writer, so the count carries across flushes. Mutates in place. A
    point already at `WritePrecision.US` was spread by an earlier flush whose
    write failed, and is already in the count: it is left exactly as it is, so
    a re-flush neither multiplies nor shifts it again.
    """

    def __init__(self):
        self._last: dict[tuple, tuple[int, int]] = {}  # series -> (base_us, seq)

    def __call__(self, points: list[Point]) -> None:
        for p in points:
            if p._write_precision == WritePrecision.US:
                continue  # spread already -- counting it again would move it
            base_us = p._time * 1_000  # milliseconds -> microseconds
            series = (p._name, tuple(sorted(p._tags.items())), tuple(sorted(p._fields)))
            last = self._last.get(series)
            seq = last[1] + 1 if last is not None and last[0] == base_us else 0
            self._last[series] = (base_us, seq)
            p.time(base_us + seq, WritePrecision.US)


# Emitted per market event, and already in the bronze layer: never recorded.
# The runtime also tells the strategy, so it need not build them at all.
UNRECORDED_MEASUREMENTS = frozenset({Measurement.MARKET_PRICE.value, Measurement.TRADE.value})


class InfluxdbWriter(BufferedWriter):
    """Buffered writer for one bucket, created if it does not exist."""

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket_name: str,
        buffer_size: int = 2048,
        param_set_id: str | None = None,
        tags: dict[str, str] | None = None,
    ):
        super(InfluxdbWriter, self).__init__(buffer_size)
        self.client = create_client(url, token, org)
        self.bucket_name = bucket_name
        self.tags = dict(tags or {})
        if param_set_id:
            self.tags["param_set_id"] = param_set_id
        self._spread_timestamps = TimestampSpreader()
        self._init_bucket()
        self.w = self.client.write_api(
            write_options=WriteOptions(
                write_type=WriteType.synchronous,
                batch_size=buffer_size,
            )
        )

    def _init_bucket(self):
        b = self.client.buckets_api()
        if b.find_bucket_by_name(self.bucket_name):
            return
        LOG.info(f"CREATE_BUCKET {self.bucket_name}")
        b.create_bucket(bucket_name=self.bucket_name)

    def write(self, points: list[Point]):
        self._spread_timestamps(points)
        for p in points:
            for k, v in self.tags.items():
                p.tag(k, v)
        self.w.write(self.bucket_name, record=points, write_precision=WritePrecision.US)

    def close(self):
        super().close()
        self.w.close()
        self.client.close()


def _regroup_pairs(flat: list) -> list[tuple]:
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]


_TAG_ENUMS = {
    Tag.DATA_TYPE: DataType,
    Tag.EXCHANGE_NAME: ExchangeName,
    Tag.ORDER_TYPE: OrderType,
    Tag.SETTLE_TYPE: SettleType,
    Tag.SIDE: Side,
    Tag.SYMBOL: Symbol,
}


def _get_tag_value(tag: Tag, value: int) -> str:
    enum = _TAG_ENUMS.get(tag)
    return enum(value).name if enum else str(value)


def convert_to_point(metric: Metric) -> Point:
    """One `Metric` as an InfluxDB Point.

    Tag and field keys arrive as enum values rather than strings, because the hot
    path packs integers. They are resolved back to names here, which is the only
    place a human ever reads them.
    """
    measurement = Measurement(metric.measurement)
    ts = metric.ts
    tags = _regroup_pairs(metric.tags)
    fields = _regroup_pairs(metric.fields)
    point = Point(measurement.name).time(int(ts * 1000), WritePrecision.MS)
    for k, v in tags:
        if type(k) is int:
            tag = Tag(k)
            k = tag.name
            v = _get_tag_value(tag, v)
        point.tag(k, v)
    for k, v in fields:
        if type(k) is int:
            k = Field(k).name
        point.field(k, v)
    return point


class InfluxdbStore:
    """A connection's worth of configuration, and the operations on it."""

    def __init__(self, token: str, write_url: str, read_url: str, org: str | None = None):
        self._token = token
        self._write_url = write_url
        self._read_url = read_url
        self._org = org

    @property
    def org(self) -> str:
        """The organisation buckets are created in.

        Given, or else the one the token can see. Asked rather than defaulted:
        the env names no organisation, and a guessed name fails only once
        the first bucket is created.
        """
        if self._org is None:
            client = create_client(self._write_url, self._token, None)
            try:
                names = [o.name for o in client.organizations_api().find_organizations()]
            finally:
                client.close()
            if len(names) != 1:
                raise ValueError(f"The InfluxDB token sees {len(names)} organisations {names}; name one")
            self._org = names[0]
        return self._org

    def create_writer(
        self,
        bucket_name: str,
        buffer_size: int = 2048,
        param_set_id: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> InfluxdbWriter:
        return InfluxdbWriter(
            self._write_url,
            self._token,
            self.org,
            bucket_name,
            buffer_size,
            param_set_id,
            tags,
        )

    def delete_tagged(self, bucket_name: str, tags: dict[str, str]) -> None:
        """Delete every point in `bucket_name` carrying all of `tags`, at any time. See the note on weekly buckets."""
        predicate = " AND ".join(f'{k}="{v}"' for k, v in tags.items())
        client = create_client(self._write_url, self._token, self.org)
        try:
            client.delete_api().delete(
                dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
                dt.datetime(2262, 1, 1, tzinfo=dt.UTC),
                predicate,
                bucket=bucket_name,
                org=self.org,
            )
        finally:
            client.close()

    def ping(self) -> bool:
        """Whether the server answers. Asked before a run, not after its replay."""
        client = create_client(self._write_url, self._token, None)
        try:
            return client.ping()
        finally:
            client.close()


class InfluxReporter(Reporter):
    """A backtest's metrics, orders and PnL, written to one bucket as they happen.

    What it records is what the strategy and the runtime report, with two
    backtest-specific rules:

    - **Nothing from the warmup period**, which is replayed for the indicators
      but is not part of the requested range -- except the open-aligned copy
      below.
    - **A closed position is written twice**, tagged `time_align=close` at its
      close and `time_align=open` at its entry, so a chart can align PnL with the
      signal that caused it or with when it settled. A position opened during
      warmup and closed in range is reported, so its open copy is earlier than
      the range.

    `MARKET_PRICE` and `TRADE` metrics are dropped: they are emitted per market
    event, are already in the bronze layer, and would outweigh everything else.
    """

    def __init__(self, writer: InfluxdbWriter, dt_range: DtRange):
        super().__init__()
        self.writer = writer
        self.dt_range = dt_range
        # Per reporter, not shared: see the note in `BacktestRuntime.__init__`.
        self._skip_measurements = frozenset(tuple(UNRECORDED_MEASUREMENTS))

    def _add(self, metric: Metric) -> None:
        self.writer.add(convert_to_point(metric))

    def report_metrics(self, metrics: list[Metric]) -> None:
        for metric in metrics:
            if metric.measurement in self._skip_measurements:
                continue
            if in_range(metric.ts, self.dt_range):
                self._add(metric)

    def report_order(self, order_response: OrderResponse) -> None:
        if in_range(order_response.get_ts(), self.dt_range):
            self._add(order_response.to_metric())

    def report_pnl(self, pnl_update: PnlUpdate) -> None:
        if pnl_update.settle_type != SettleType.CLOSE or not in_range(pnl_update.get_ts(), self.dt_range):
            return
        for align, ts in (("close", pnl_update.get_ts()), ("open", pnl_update.open_ts)):
            metric = pnl_update.to_metric()
            metric.ts = ts
            self._add(metric.tag("time_align", align))

    def stop(self) -> None:
        self.writer.close()


# --------------------------------------------------------------------------
# Enoshima's own layer: what bucket a run belongs in.
# --------------------------------------------------------------------------

BUCKET_PREFIX = "backtest"


def bucket_name(param_set_id: str, batch_id: int) -> str:
    return f"{BUCKET_PREFIX}_{param_set_id}_{batch_id}"


def weekly_bucket_name(param_set_id: str, run_id: str) -> str:
    return f"{BUCKET_PREFIX}_{param_set_id}_w{run_id}"
