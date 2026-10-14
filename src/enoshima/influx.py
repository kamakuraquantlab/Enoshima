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
precision, and colliding timestamps are spread by 1us each, because InfluxDB treats
measurement + tags + timestamp as a primary key and would otherwise overwrite
rather than append. Two fills inside the same millisecond are ordinary, so this is
load-bearing rather than defensive. It is also deterministic, so re-running a
parameter set over the same range overwrites its points rather than doubling them.

## One bucket per parameter set

`backtest_<param_set_id>`, the id naming the environment and the parameters (see
`param_id`), so an optimize sweep's combinations sit side by side and a dashboard
can switch between them with a variable rather than a query rewrite. A temporary
run's id ends in `_tmp_<timestamp>`, so it writes a bucket of its own.

Every bucket is kept. Enoshima creates buckets and writes to them, and never
expires or deletes one: cleaning up is a separate tool's job. A retention on
temporary buckets used to make InfluxDB refuse any point older than 30 days,
which is every point of a backtest of last year.
"""

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

from .result import in_range

LOG = getLogger(__name__)

WRITE_TIMEOUT_MS = 120_000  # large batches from a finished backtest are slow


def create_client(url: str, token: str, org: str | None) -> InfluxDBClient:
    return InfluxDBClient(url, token, org=org, timeout=WRITE_TIMEOUT_MS)


def normalize_points_timestamp_us(points: list[Point]) -> None:
    """Move points to microseconds and break timestamp ties.

    Mutates in place. A point already at `WritePrecision.US` is left at its
    scale rather than multiplied again, so re-flushing a buffer after a failed
    write cannot overflow the timestamps.
    """
    last_base_us = None
    seq = 0

    for p in points:
        if p._write_precision == WritePrecision.US:
            base_us = p._time  # already microseconds -- do not multiply
        else:
            base_us = p._time * 1_000  # milliseconds -> microseconds

        if base_us == last_base_us:
            seq += 1
        else:
            seq = 0
            last_base_us = base_us

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
    ):
        super(InfluxdbWriter, self).__init__(buffer_size)
        self.client = create_client(url, token, org)
        self.bucket_name = bucket_name
        self.param_set_id = param_set_id
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
        normalize_points_timestamp_us(points)
        if self.param_set_id:
            for p in points:
                p.tag("param_set_id", self.param_set_id)
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
    ) -> InfluxdbWriter:
        return InfluxdbWriter(
            self._write_url,
            self._token,
            self.org,
            bucket_name,
            buffer_size,
            param_set_id,
        )

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


def bucket_name(param_set_id: str) -> str:
    return f"{BUCKET_PREFIX}_{param_set_id}"
