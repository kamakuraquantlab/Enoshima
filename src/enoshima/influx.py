"""Optional: load a finished run's metrics into InfluxDB, for Grafana.

A backtest never touches this. It writes its metrics to a local `MetricLog` and
finishes; this is the separate step that copies them into a bucket so they can be
charted, and it can be repeated as often as you like because the files are the
record.

Requires the extra: `pip install 'kamakuraquantlab-enoshima[influx]'`, plus a
`~/.qate/influxdb.keys` holding the token and URLs.

## Why this is here and not in qate

`qate` writes the metric log and stops there. Sending it anywhere is a decision
about infrastructure, and whoever wants the charts owns it -- which for a backtest
is Enoshima. That is also what keeps `influxdb-client` out of `qate`'s dependency
tree entirely.

## Timestamps

Metric records carry float epoch seconds. Points are written at microsecond
precision, and colliding timestamps are spread by 1us each, because InfluxDB treats
measurement + tags + timestamp as a primary key and would otherwise overwrite
rather than append. Two fills inside the same millisecond are ordinary, so this is
load-bearing rather than defensive.

## One bucket per parameter set

`backtest_<ENV>_<param_set_id>`, so an optimize sweep's combinations sit side by
side and a dashboard can switch between them with a variable rather than a query
rewrite. Runs marked temporary get `_tmp_<batch_id>` appended and a 30-day
retention, because a quick idea test should not need remembering to clean up after;
`delete_tmp_buckets` removes the expired ones.
"""

import time
from logging import getLogger
from pathlib import Path
from typing import Iterable

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import WriteOptions, WriteType
from influxdb_client.domain import BucketRetentionRules
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
from qate.core.order import OrderType
from qate.core.symbol import Symbol
from qate.trading.metrics import metrics_files, read_metrics_dir
from qate.util.buffer import BufferedWriter

from .result import METRICS_PREFIX

LOG = getLogger(__name__)

WRITE_TIMEOUT_MS = 120_000  # large batches from a finished backtest are slow


def create_client(url: str, token: str, org: str) -> InfluxDBClient:
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


class InfluxdbWriter(BufferedWriter):
    """Buffered writer for one bucket, created if it does not exist."""

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket_name: str,
        buffer_size: int = 2048,
        retention_seconds: int | None = None,
        param_set_id: str | None = None,
    ):
        super(InfluxdbWriter, self).__init__(buffer_size)
        self.client = create_client(url, token, org)
        self.bucket_name = bucket_name
        self.retention_seconds = retention_seconds
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
        if self.retention_seconds is None:
            b.create_bucket(bucket_name=self.bucket_name)
        else:
            rules = BucketRetentionRules(type="expire", every_seconds=self.retention_seconds)
            b.create_bucket(bucket_name=self.bucket_name, retention_rules=rules)

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
    """A connection's worth of configuration, and the operations on it.

    `config` is the `(token, write_url, read_url)` triple
    `qate_env.sys_env.get_influxdb` returns.
    """

    def __init__(self, config: tuple[str, str, str], org: str = "qate"):
        (token, write_url, read_url) = config
        self._token = token
        self._write_url = write_url
        self._read_url = read_url
        self._org = org

    def create_writer(
        self,
        bucket_name: str,
        buffer_size: int = 2048,
        retention_seconds: int | None = None,
        param_set_id: str | None = None,
    ) -> InfluxdbWriter:
        return InfluxdbWriter(
            self._write_url,
            self._token,
            self._org,
            bucket_name,
            buffer_size,
            retention_seconds,
            param_set_id,
        )

    def delete_bucket(self, bucket_name: str) -> None:
        """Drop a bucket and everything in it."""
        client = create_client(self._write_url, self._token, self._org)
        try:
            b = client.buckets_api()
            bucket = b.find_bucket_by_name(bucket_name)
            if bucket:
                b.delete_bucket(bucket)
                LOG.info(f"DELETE_BUCKET {bucket_name}")
            else:
                LOG.info(f"DELETE_BUCKET {bucket_name} - not found, skipping")
        finally:
            client.close()

    def delete_range(self, bucket_name: str, start: str, stop: str) -> None:
        """Delete a time range from a bucket. `start`/`stop` are RFC3339."""
        client = create_client(self._write_url, self._token, self._org)
        try:
            client.delete_api().delete(
                start=start,
                stop=stop,
                predicate="",
                bucket=bucket_name,
                org=self._org,
            )
            LOG.info(f"DELETE_RANGE bucket={bucket_name} {start} -> {stop}")
        finally:
            client.close()

    def list_buckets(self) -> list[str]:
        client = create_client(self._write_url, self._token, self._org)
        try:
            return [b.name for b in client.buckets_api().find_buckets().buckets]
        finally:
            client.close()


def export_metric_objects(
    metrics: Iterable[Metric],
    writer: InfluxdbWriter,
    batch_size: int = 2048,
) -> int:
    """Write metrics through a writer. Returns how many were written."""
    count = 0
    points: list[Point] = []
    for metric in metrics:
        points.append(convert_to_point(metric))
        count += 1
        if len(points) >= batch_size:
            writer.write(points)
            points = []
    if points:
        writer.write(points)
    return count


def export_metrics(
    directory: str | Path,
    store: InfluxdbStore,
    bucket_name: str,
    prefix: str = "",
    retention_seconds: int | None = None,
) -> int:
    """Load a directory of local msgpack metrics into a bucket.

    The second half of the local-first arrangement described in
    `qate.trading.metrics`. Returns the number of metrics written.
    """
    writer = store.create_writer(bucket_name, retention_seconds=retention_seconds)
    try:
        count = export_metric_objects(read_metrics_dir(directory, prefix), writer)
    finally:
        writer.close()
    LOG.info(f"Exported {count} metric objects from {directory} to bucket {bucket_name}")
    return count


# --------------------------------------------------------------------------
# Enoshima's own layer: where a run's files are, and what bucket they belong in.
# --------------------------------------------------------------------------

BUCKET_PREFIX = "backtest"
TMP_MARKER = "_tmp_"
TMP_RETENTION_SECONDS = 30 * 24 * 60 * 60


def bucket_name(env_name: str, param_set_id: str, batch_id: int | None = None) -> str:
    base = f"{BUCKET_PREFIX}_{env_name}_{param_set_id}"
    if batch_id is None:
        return base
    return f"{base}{TMP_MARKER}{batch_id}"


def export_run(
    result_dir: str | Path,
    store: InfluxdbStore,
    bucket: str,
    retention_seconds: int | None = None,
) -> int:
    """Load one run's metrics directory into a bucket. Returns objects written."""
    result_dir = Path(result_dir)
    if not metrics_files(result_dir, METRICS_PREFIX):
        LOG.warning(f"No metrics files under {result_dir}; nothing to export")
        return 0
    return export_metrics(result_dir, store, bucket, prefix=METRICS_PREFIX, retention_seconds=retention_seconds)


def export_env(env_work_dir: str | Path, store: InfluxdbStore, env_name: str, batch_id: int | None = None) -> int:
    """Export every run under an environment, or just one batch of them.

    The layout it walks is `result/<batch_id>/<param_set_id>/`, which is what
    `Runner.result_dir` writes.
    """
    root = Path(env_work_dir) / "result"
    if not root.is_dir():
        LOG.warning(f"No results under {root}")
        return 0

    batch_dirs = [root / str(batch_id)] if batch_id is not None else sorted(d for d in root.iterdir() if d.is_dir())
    total = 0
    for batch_dir in batch_dirs:
        if not batch_dir.is_dir():
            LOG.warning(f"No such batch: {batch_dir}")
            continue
        for param_dir in sorted(d for d in batch_dir.iterdir() if d.is_dir()):
            bucket = bucket_name(env_name, param_dir.name)
            written = export_run(param_dir, store, bucket)
            LOG.info(f"{param_dir.name}: {written} metric objects -> {bucket}")
            total += written
    return total


def delete_tmp_buckets(store: InfluxdbStore, days_ago: int = 7) -> int:
    """Drop `_tmp_` buckets whose embedded batch id is older than `days_ago`.

    The batch id in the name is a unix timestamp, so the age is readable from the
    bucket without asking InfluxDB when it was created. A name that does not
    parse is left alone: it was not created by this tool.
    """
    cutoff = time.time() - days_ago * 86400
    deleted = 0
    for name in store.list_buckets():
        idx = name.rfind(TMP_MARKER)
        if idx == -1:
            continue
        try:
            batch_id = int(name[idx + len(TMP_MARKER) :])
        except ValueError:
            continue
        if batch_id >= cutoff:
            continue
        LOG.info(f"Deleting {name}")
        store.delete_bucket(name)
        deleted += 1
    LOG.info(f"Deleted {deleted} temporary bucket(s) older than {days_ago} days")
    return deleted
