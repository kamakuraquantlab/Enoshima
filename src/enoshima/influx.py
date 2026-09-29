"""Optional: load a finished run's metrics into InfluxDB, for Grafana.

A backtest never touches this. It writes its metrics to local msgpack and
finishes; this is the separate step that copies them into a bucket so they can be
charted, and it can be repeated as often as you like because the files are the
record.

Requires the extra: `pip install 'kamakuraquantlab-enoshima[influx]'`, plus a
`~/.qate/influxdb.keys` holding the token and URLs.

## One bucket per parameter set

`backtest_<ENV>_<param_set_id>`, so an optimize sweep's combinations sit side by
side and a dashboard can switch between them with a variable rather than a query
rewrite. Runs marked temporary get `_tmp_<batch_id>` appended and a 30-day
retention, because a quick idea test should not need remembering to clean up
after; `delete_tmp_buckets` removes the expired ones.
"""

import time
from logging import getLogger
from pathlib import Path

from qate.store.influx import InfluxdbStore, export_metrics
from qate.store.metrics import metrics_files

from .result import METRICS_PREFIX

LOG = getLogger(__name__)

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
