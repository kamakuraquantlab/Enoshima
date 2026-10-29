"""The results viewer: `enoshima web`, a Flask API and a React page over what runs wrote to the lake.

It reads, and replays nothing. Every env with a finished run in the data lake
is listed (`lake`): a run is one with its `BacktestRun` row, a training a
finished `post_analysis/train/<train_id>/` in the env's analysis folder there.
A run is served as the files its folder once held -- `meta.json`,
`summary.json`, `trades.csv` and `by_<grouping>.json`, the groupings computed
from its trades -- and its records as `orders.csv`, `pnl.csv`, `bars.csv` and
`metrics.csv`. The one change it makes is deleting a run's files when asked.
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

import csv
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from enoshima.best import BEST_RESULT_DIR, SELECTION_FILE
from enoshima.lake import Store
from enoshima.ledger import GROUPINGS
from enoshima.post_analysis import walk_forward

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
# What `enoshima walk-forward --train` writes per group, under post_analysis/train/<id>/.
TRAIN_FILES = set(walk_forward.TRAIN_FILES)
TRAIN_DIR = Path(walk_forward.OUTPUT_DIR) / walk_forward.TRAIN_DIR


def _is_finished_dir(path):
    # A finished training: summary.json is written last, and a whole folder renamed
    # into place, so a .tmp-* folder is one still being written.
    return (
        path.is_dir()
        and not path.is_symlink()
        and "." not in path.name
        and (path / "meta.json").is_file()
        and (path / "summary.json").is_file()
    )


def _recommendations(store):
    """run_id -> the batch ids whose best_result recommended it."""
    out = {}
    best = store.analysis_dir / BEST_RESULT_DIR
    if best.is_dir():
        for batch in best.iterdir():
            if batch.is_dir():
                for copy in batch.glob("*.json"):
                    if copy.name != SELECTION_FILE:
                        out.setdefault(copy.stem, []).append(batch.name)
    return {run_id: sorted(batches) for run_id, batches in out.items()}


def _run_trainings(store):
    """run_id -> (train_id, runs.json) of the newest training whose group held that run.

    A training's runs.json compares each walk-forward rule with each of its
    group's runs held, so it is that run's page; an older one is superseded.
    """
    out, created = {}, {}
    base = store.analysis_dir / TRAIN_DIR
    if not base.is_dir():
        return out
    for path in base.iterdir():
        if not _is_finished_dir(path) or not (path / "runs.json").is_file():
            continue
        try:
            when = json.loads((path / "meta.json").read_text()).get("created_ts") or 0
            against = json.loads((path / "runs.json").read_text())
        except (ValueError, OSError, AttributeError):
            app.logger.warning("Cannot read training folder: %s", path)
            continue
        for run_id in against.get("runs", {}):
            if when >= created.get(run_id, float("-inf")):
                out[run_id], created[run_id] = (path.name, against), when
    return out


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


def _walk_forward_columns(train_id, against):
    best = next((r for r in against.get("rules", []) if r.get("key") == against.get("best")), None)
    return {"train_id": train_id, "best_pnl5": best and best.get("pnl5"), "best_rule": best and best.get("label")}


def _iter_run_logs(env_filter=None):
    for env in _envs():
        if env_filter and env != env_filter:
            continue
        store = _store(env)
        recommended = _recommendations(store)
        trainings = _run_trainings(store)
        for run_id in store.run_ids():
            try:
                run = store.read_run(run_id)
                data, summary = run["meta"], run["summary"]
                if not isinstance(data, dict) or not isinstance(summary, dict):
                    continue
                data["result"] = summary
                data["run_id"] = run_id
                data["env_name"] = env
                data["source"] = env  # the page names a run by source and id; the env is its last segment
                data["grafana_url"] = _grafana_url(data)
                data["recommended_in"] = ",".join(recommended.get(run_id, [])) or None
                if run_id in trainings:
                    data["walk_forward"] = _walk_forward_columns(*trainings[run_id])
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


def _find_training(env, train_id):
    """A finished training folder for a request, or an error response."""
    if env not in _envs():
        return None, (jsonify(error="Environment not found"), 404)
    if not _valid_id(train_id):
        return None, (jsonify(error="Invalid id"), 400)
    base = (_store(env).analysis_dir / TRAIN_DIR).resolve()
    path = base / train_id
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        return None, (jsonify(error="Invalid path"), 400)
    if not _is_finished_dir(path):
        return None, (jsonify(error="Not found"), 404)
    return path, None


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


def _training_detail(path):
    """A training folder's meta and summary, and which of its files it has."""
    try:
        meta = json.loads((path / "meta.json").read_text())
        summary = json.loads((path / "summary.json").read_text())
    except (ValueError, OSError):
        return jsonify(error="Could not read folder"), 400
    files = sorted(p.name for p in path.iterdir() if p.name in TRAIN_FILES)
    return jsonify(_json_safe({"train_id": path.name, "meta": meta, "summary": summary, "files": files}))


def _training_file(path, file_name):
    if file_name not in TRAIN_FILES:
        return jsonify(error="Not a served file"), 400
    target = path / file_name
    if target.is_symlink() or not target.is_file():
        return jsonify(error="File not found"), 404
    if file_name.endswith(".json"):
        try:
            return jsonify(_json_safe(json.loads(target.read_text())))
        except (ValueError, OSError):
            return jsonify(error="Could not read file"), 400
    return send_from_directory(path, file_name, mimetype="text/csv")


def _iter_trainings(env_filter=None):
    for env in _envs():
        if env_filter and env != env_filter:
            continue
        base = _store(env).analysis_dir / TRAIN_DIR
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if not _is_finished_dir(path):
                continue
            try:
                data = json.loads((path / "meta.json").read_text())
                summary = json.loads((path / "summary.json").read_text())
                if not isinstance(data, dict) or not isinstance(summary, dict):
                    continue
                data["result"] = summary
                data["train_id"] = path.name
                data["env_name"] = env
                data["source"] = env  # the page names a run by source and id; the env is its last segment
                yield _json_safe(data)
            except (ValueError, OSError):
                LOG.warning(f"Cannot read training folder: {path}")


@app.get("/api/envs")
def api_envs():
    return jsonify(_envs())


@app.get("/api/results")
def api_results():
    def timestamp(row):
        value = row.get("start_ts")
        return value if isinstance(value, (int, float)) else 0

    return jsonify(
        sorted(_iter_run_logs(request.args.get("env")), key=timestamp, reverse=True)
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


@app.get("/api/runs/<env>/<run_id>/walk-forward")
def api_run_walk_forward(env, run_id):
    """The walk-forward rules against this run held: the newest training of its group, cut to this run."""
    store, error = _find_run(env, run_id)
    if error:
        return error
    found = _run_trainings(store).get(run_id)
    if found is None:
        return jsonify(error="No walk-forward training includes this run"), 404
    train_id, against = found
    try:
        meta = json.loads((store.analysis_dir / TRAIN_DIR / train_id / "meta.json").read_text())
    except (ValueError, OSError):
        return jsonify(error="Could not read training"), 400
    held = against["runs"][run_id]
    rules = [{**rule, "held": held.get("rules", {}).get(rule.get("key"))} for rule in against.get("rules", [])]
    return jsonify(_json_safe({"train_id": train_id, "run_id": run_id, "meta": meta, "params": held.get("params"),
                               "best": against.get("best"), "rules": rules}))


@app.get("/api/trainings")
def api_trainings():
    """Every finished walk-forward training, meta.json with summary.json as `result`, newest first."""
    def created(row):
        value = row.get("created_ts")
        return value if isinstance(value, (int, float)) else 0

    return jsonify(sorted(_iter_trainings(request.args.get("env")), key=created, reverse=True))


@app.get("/api/trainings/<env>/<train_id>")
def api_training(env, train_id):
    path, error = _find_training(env, train_id)
    return error or _training_detail(path)


@app.get("/api/trainings/<env>/<train_id>/files/<file_name>")
def api_training_file(env, train_id, file_name):
    path, error = _find_training(env, train_id)
    return error or _training_file(path, file_name)


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
