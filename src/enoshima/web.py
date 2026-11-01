"""The results viewer: `enoshima web`, a Flask API and a React page over what runs wrote to the lake.

It reads, and replays nothing. Every env with a finished run in the data lake
is listed (`lake`): a run is one with its `BacktestRun` row. A run is served as the files its folder once held -- `meta.json`,
`summary.json`, `trades.csv` and `by_<grouping>.json`, the groupings computed
from its trades -- and its records as `orders.csv`, `pnl.csv`, `bars.csv` and
`metrics.csv`. The one change it makes is deleting a run's files when asked.
`/api/results` with a `start` and an `end` gives each run's `result` over
those JST days instead: `weekly.summarize` of its trades closed in them and
its fills' slippage there, the function a weekly run's row is built with.
The one other read is `/api/prices`, a market's recorded trades through hase,
beside a run's equity.

The lake is komachi's root, as for every other command. The viewer runs in its
own env, `ENOSHIMA_API` under qate-env's root, created by `enoshima web --setup`:

    config.json              port
    grafana_dashboard.json   optional links from a run to a dashboard of its own

A dashboard matches a run when every key of its `match` equals that key of the
run's flattened meta (`trading.module_name`, `config.market`, ...). The
first that matches and has a value for every `{key}` in its url and query
gives the run its link; see `_grafana_url`. The viewer names no strategy and no
dashboard: those are the env's.

The page is `web/` in a checkout, built to `web/dist` with
`npm run build --prefix web`; this module serves it from there.
"""

import bisect
import csv
import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from logging import getLogger
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory
from hase import bronze, store
from hase.bronze import DataType
from qate.core.model import Market
from qate_env.env import Env
from qate_env.env_name import EnvName

from enoshima import lake
from enoshima.lake import Store
from enoshima.ledger import GROUPINGS, SUFFIXES
from enoshima.weekly import RATES, summarize

LOG = getLogger(__name__)

ENV_NAME = EnvName("ENOSHIMA_API")
CONFIG_FILE = "config.json"
GRAFANA_FILE = "grafana_dashboard.json"
WEB_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"


@dataclass
class Config:
    port: int = 9768


app = Flask(__name__)
# Set by `serve`; the tests set them directly.
LAKE_ROOT = Path(".")
GRAFANA_DASHBOARDS = []


def _envs():
    return lake.envs(LAKE_ROOT)


def _store(env):
    return Store(LAKE_ROOT, env)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


# What a run is served as: its meta and summary, its trades and their groupings
# as the folder files they once were, and its records as CSV.
JSON_FILES = {"meta.json", "summary.json"} | {f"by_{name}.json" for name in GROUPINGS}
RECORD_FILES = {"orders.csv": lake.ORDER, "pnl.csv": lake.PNL, "bars.csv": lake.BAR, "metrics.csv": lake.METRIC}
RUN_FILES = JSON_FILES | {"trades.csv"} | set(RECORD_FILES)


def _flatten(value, prefix=""):
    out = {}
    for key, item in value.items():
        if isinstance(item, dict):
            out.update(_flatten(item, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = item
    return out


def _jst_ms(text):
    when = datetime.fromisoformat(text)
    return round((when if when.tzinfo else when.replace(tzinfo=JST)).timestamp() * 1000)


def _dashboards(config):
    """grafana_dashboard.json's `dashboards`, each a `match`, a `url` and a `query`."""
    dashboards = (config or {}).get("dashboards", [])
    for dashboard in dashboards:
        if not isinstance(dashboard.get("url"), str) or not all(
            isinstance(dashboard.get(key, {}), dict) for key in ("match", "query")
        ):
            raise ValueError(f"{GRAFANA_FILE}: each dashboard needs a url, and match and query objects")
    return dashboards


def _grafana_url(meta):
    """The first matching dashboard's link for a run, or None.

    A dashboard matches when every `match` key of the flattened meta equals its
    value. `{key}` in its url and query is that meta value, or one of exchange,
    symbol and base (from config.market) or start_ms and end_ms (the JST range);
    a dashboard with a placeholder the run has no value for gives no link.
    """
    fields = _flatten(meta)
    market = fields.get("config.market")
    if isinstance(market, str) and ":" in market:
        fields["exchange"], fields["symbol"] = market.split(":", 1)
        fields["base"] = fields["symbol"].split("_")[0]
    for key in ("start", "end"):
        try:
            fields[f"{key}_ms"] = _jst_ms(fields[f"{key}_str"])
        except (KeyError, TypeError, ValueError):
            pass

    def fill(template):
        def value(match):
            item = fields.get(match.group(1))
            if item is None or item == "":
                raise KeyError(match.group(1))
            return str(item)
        return re.sub(r"\{([\w.]+)\}", value, str(template))

    for dashboard in GRAFANA_DASHBOARDS:
        if any(fields.get(k) != v for k, v in dashboard.get("match", {}).items()):
            continue
        try:
            url = fill(dashboard["url"])
            query = {k: fill(v) for k, v in dashboard.get("query", {}).items()}
        except KeyError:
            continue
        return f"{url}?{urlencode(query)}" if query else url
    return None


@dataclass(frozen=True)
class Window:
    """JST days `first` to `last`, both included: a range the table's results are summarized over."""

    first: date | None
    last: date | None

    @classmethod
    def from_args(cls, args):
        """From `start` and `end` (`YYYY-MM-DD`, either may be left out), or None for the whole run."""
        first, last = (date.fromisoformat(args[k]) if args.get(k) else None for k in ("start", "end"))
        if first is None and last is None:
            return None
        if first and last and last < first:
            raise ValueError("end before start")
        return cls(first, last)

    def bounds(self):
        """[start_ts, end_ts) in epoch seconds."""
        def midnight(day):
            return datetime.combine(day, datetime.min.time(), JST).timestamp()
        return (midnight(self.first) if self.first else float("-inf"),
                midnight(self.last + timedelta(days=1)) if self.last else float("inf"))

    def covered_by(self, meta):
        """Whether the run's own range holds every day of the window."""
        start, end = str(meta.get("start_str") or "")[:10], str(meta.get("end_str") or "")[:10]
        return bool(start and end) and (self.first is None or start <= self.first.isoformat()) and (
            self.last is None or end >= self.last.isoformat())


# The figures that are averages over orders, not trades.
PER_ORDER = ("slippage", "slippage_rate")


@lru_cache(maxsize=1024)
def _window_inputs(env, run_id, written):
    """A run's trades in close order with their close times, and its fills in time order as
    (times, slippages, notionals), or None if it recorded none. `written` is its row's mtime,
    so a re-run is re-read.
    """
    store = _store(env)
    trades = store.trades(run_id)
    orders = store.records(lake.ORDER, run_id)
    fills = None
    if orders is not None:
        ts, slippage, price, size = (orders.column(c).to_numpy(zero_copy_only=False).astype(float)
                                     for c in ("ts", "SLIPPAGE", "EXEC_PRICE", "EXEC_SIZE"))
        order = np.argsort(ts, kind="stable")
        fills = ts[order], slippage[order], (price * size)[order]
    return [t.close_ts for t in trades], trades, fills


def _order_stats(fills, start, end):
    """`Evaluation.order_stats` of the fills in [start, end): its counters, from the recorded fills."""
    ts, slippage, notional = fills
    keep = slice(np.searchsorted(ts, start), np.searchsorted(ts, end))
    slippage, notional = slippage[keep], notional[keep]
    stats = {}
    for s, rate in RATES.items():
        extra = slippage + notional * rate
        stats.update({f"order_cnt{s}": len(extra), f"order_slippage{s}": float(extra.sum()),
                      f"order_slippage_rate{s}": float((extra / notional).sum())})
    return stats


def _window_summary(store, run_id, window):
    """The run's summary over `window`: `summarize` of the trades that closed in it, as the run page
    narrows, with the slippage of the fills in it."""
    closes, trades, fills = _window_inputs(store.env, run_id, store.path(lake.RUN, run_id).stat().st_mtime_ns)
    start, end = window.bounds()
    trades = trades[bisect.bisect_left(closes, start):bisect.bisect_left(closes, end)]
    summary = summarize(trades, {} if fills is None else _order_stats(fills, start, end))
    if fills is None and trades:
        # A run imported from before the lake recorded no fills: its slippage is unknown, not zero.
        summary.update({f"{name}{s}": None for name in PER_ORDER for s in SUFFIXES})
    return summary


def _iter_run_logs(env_filter=None, window=None):
    for env in _envs():
        if env_filter and env != env_filter:
            continue
        store = _store(env)
        for run_id in store.run_ids():
            try:
                run = store.read_run(run_id)
                data, summary = run["meta"], run["summary"]
                if not isinstance(data, dict) or not isinstance(summary, dict):
                    continue
                if window is not None:
                    data["result"] = _window_summary(store, run_id, window)
                    data["range_covered"] = window.covered_by(data)
                else:
                    data["result"] = summary
                data["run_id"] = run_id
                data["env_name"] = env
                data["source"] = env  # the page names a run by source and id; the env is its last segment
                data["grafana_url"] = _grafana_url(data)
                yield _json_safe(data)
            except (ValueError, OSError):
                LOG.warning(f"Cannot read run {run_id} of {env}")


def _valid_id(item_id):
    return bool(item_id) and Path(item_id).name == item_id and "." not in item_id


def _find_run(env, run_id):
    """A finished run for a request, as its store, or an error response."""
    if env not in _envs():
        return None, (jsonify(error="Environment not found"), 404)
    if not _valid_id(run_id):
        return None, (jsonify(error="Invalid id"), 400)
    store = _store(env)
    if not store.is_complete(run_id):
        return None, (jsonify(error="Not found"), 404)
    return store, None


def _run_files(store, run_id):
    """The files a run has: every JSON and its trades, and each record dataset with rows."""
    counts = {}
    for part in store.parts(run_id):
        for dataset, n in store.read_part(run_id, part)["counts"].items():
            counts[dataset] = counts.get(dataset, 0) + n
    return sorted(JSON_FILES | {"trades.csv"} | {f for f, d in RECORD_FILES.items() if counts.get(d)})


def _csv(table):
    out = io.StringIO()
    if table is not None:
        writer = csv.writer(out)
        writer.writerow(table.column_names)
        for row in zip(*(column.to_pylist() for column in table.columns)):
            writer.writerow(["" if v is None else v for v in row])
    return Response(out.getvalue(), mimetype="text/csv")


def _run_file(store, run_id, file_name):
    if file_name not in RUN_FILES:
        return jsonify(error="Not a served file"), 400
    try:
        if file_name == "meta.json" or file_name == "summary.json":
            return jsonify(_json_safe(store.read_run(run_id)[file_name[:-5]]))
        if file_name in RECORD_FILES:
            return _csv(store.records(RECORD_FILES[file_name], run_id))
        if file_name == "trades.csv":
            return _csv(lake.trades_table(store.trades(run_id)))
        return jsonify(_json_safe(store.grouping(run_id, file_name[3:-5])))
    except (ValueError, OSError) as e:
        LOG.warning(f"Cannot read {file_name} of run {run_id}: {e}")
        return jsonify(error="Could not read run"), 400


@app.get("/api/envs")
def api_envs():
    return jsonify(_envs())


@app.get("/api/results")
def api_results():
    """Every finished run, newest first; with `start` and/or `end`, each `result` is over those JST days."""
    def timestamp(row):
        value = row.get("start_ts")
        return value if isinstance(value, (int, float)) else 0

    try:
        window = Window.from_args(request.args)
    except ValueError:
        return jsonify(error="Give start and end as YYYY-MM-DD, end not before start"), 400
    return jsonify(
        sorted(_iter_run_logs(request.args.get("env"), window), key=timestamp, reverse=True)
    )


@app.get("/api/runs/<env>/<run_id>")
def api_run(env, run_id):
    """A run's meta and summary, and which files it can be served as."""
    store, error = _find_run(env, run_id)
    if error:
        return error
    try:
        run = store.read_run(run_id)
        files = _run_files(store, run_id)
    except (ValueError, OSError):
        return jsonify(error="Could not read run"), 400
    meta = run["meta"]
    return jsonify(_json_safe({"run_id": run_id, "meta": meta, "summary": run["summary"], "files": files,
                               "grafana_url": _grafana_url(meta) if isinstance(meta, dict) else None}))


@app.get("/api/runs/<env>/<run_id>/files/<file_name>")
def api_run_file(env, run_id, file_name):
    store, error = _find_run(env, run_id)
    return error or _run_file(store, run_id, file_name)


@app.delete("/api/results/<env>/<run_id>")
def delete_result(env, run_id):
    store, error = _find_run(env, run_id)
    if error:
        return error
    try:
        store.delete_run(run_id)
    except OSError:
        return jsonify(error="Could not delete run"), 400
    return "", 204


# A market's price beside a run's equity, from the recorded trades hase reads.
JST_OFFSET = 9 * 3600
JST = timezone(timedelta(seconds=JST_OFFSET))
MINUTE = 60
PRICE_STEPS = (60, 300, 900, 1800, 3600, 4 * 3600, 86400)
MAX_PRICE_POINTS = 5000


@lru_cache(maxsize=8192)
def _minute_prices(market_id, day):
    """A complete JST day's last trade price per minute, as (minute starts, prices).

    Hase reads the day; a year of one market is about two seconds the first
    time and a cache hit after, at 1,440 points a day.
    """
    frame = store.load_trades(bronze.data_root(), market_id, day)
    if frame.empty:
        return np.empty(0), np.empty(0)
    minute = (frame["ts"].to_numpy() // MINUTE) * MINUTE
    # Bronze is in timestamp order, so a minute's last row is its last trade.
    last = np.flatnonzero(np.r_[minute[1:] != minute[:-1], True])
    return minute[last], frame["price"].to_numpy()[last]


def _price_step(span):
    return next((step for step in PRICE_STEPS if span / step <= MAX_PRICE_POINTS), PRICE_STEPS[-1])


@app.get("/api/prices")
def api_prices():
    """`market`'s last trade price per step from `start` to `end` (epoch seconds).

    The step is the finest that keeps the series to MAX_PRICE_POINTS. Days not
    complete on disk are left out, so the series can have gaps.
    """
    try:
        market = Market.from_str(request.args["market"])
        start, end = float(request.args["start"]), float(request.args["end"])
    except (KeyError, ValueError):
        return jsonify(error="Give market as EXCHANGE:SYMBOL, and start and end as epoch seconds"), 400
    if not end > start:
        return jsonify(error="end must be after start"), 400

    first = datetime.fromtimestamp(start, JST).date().isoformat()
    last = datetime.fromtimestamp(end, JST).date().isoformat()
    days = [day for day in bronze.dates(market, DataType.TRADE) if first <= day <= last]
    step = _price_step(end - start)
    out = []
    for day in days:
        minutes, prices = _minute_prices(market.id, day)
        keep = (minutes >= start) & (minutes < end)
        minutes, prices = minutes[keep], prices[keep]
        if not len(minutes):
            continue
        # Steps start at JST midnight, so a day's step is the day.
        bucket = (minutes + JST_OFFSET) // step
        ends = np.flatnonzero(np.r_[bucket[1:] != bucket[:-1], True])
        # Each point at its last trade's minute: the price it shows is that minute's.
        out.extend(zip((minutes[ends] * 1000).tolist(), prices[ends].tolist()))
    return jsonify({"market": market.id, "step": step, "days": len(days), "prices": out})


@app.route("/api/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unknown_api(path):
    return jsonify(
        error="Endpoint unavailable; the viewer runs nothing, the enoshima command does"
    ), 404


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    candidate = (WEB_DIR / path).resolve()
    if candidate.is_relative_to(WEB_DIR.resolve()) and candidate.is_file():
        return send_from_directory(WEB_DIR, path)
    return send_from_directory(WEB_DIR, "index.html")


def serve(env: Env) -> None:
    """Serve the viewer with `env`'s configuration until stopped."""
    global LAKE_ROOT, GRAFANA_DASHBOARDS
    config = env.load_object(CONFIG_FILE, Config) or Config()
    LAKE_ROOT = lake.lake_root()
    GRAFANA_DASHBOARDS = _dashboards(env.load_object(GRAFANA_FILE))
    LOG.info(f"Serving {LAKE_ROOT} on port {config.port}, {len(GRAFANA_DASHBOARDS)} dashboard(s)")
    app.run(host="0.0.0.0", port=config.port, debug=False)
