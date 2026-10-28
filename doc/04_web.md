# The results viewer: `enoshima web`

```bash
pip install -e '.[web]'                 # Flask; the engine itself needs none
npm install --prefix web && npm run build --prefix web
enoshima web --setup                    # once: the viewer's own env, ENOSHIMA_API
enoshima web                            # http://localhost:9768
```

Every run folder and walk-forward training the other commands wrote, as one
page: a table of all runs, a chart page per run, and the trainings. Like
`select` and `walk-forward`, it replays nothing; it reads the folders on disk.
It launches nothing either: there is no endpoint that runs a backtest, changes
a parameter or writes a config. The one thing it changes is a run folder it is
asked to delete. The module docstring (`enoshima.web`) is the reference.

## Its env, `ENOSHIMA_API`

The viewer runs in an env of its own under qate-env's root, like every other
command, so its log, its lock (one viewer per root) and its configuration sit
in one folder. `--setup` writes:

```text
ENOSHIMA_API/
├── config.json              {"port": 9768, "results_dirs": ["."]}
├── grafana_dashboard.json   {"dashboards": []}
└── env.log                  once it has run
```

`results_dirs` are the folders under the root that envs are looked for in.
An env is listed when it has a `run_log/`, so the viewer's own env never is.
With envs kept in a subfolder as well, `~/env/backtest/NAME`, it is
`[".", "backtest"]`. Each run carries its `source`, the env's path under the
root, so two envs of one name in different folders stay apart. Both files are
read once at start.

## What it shows

**The table** is every finished run, newest first: meta.json flattened into
columns, and summary.json as `result.*`. Envs are tabs; a keyword search
checks every field; clicking a batch id filters by it; headers sort on several
columns; × hides a column, and the hidden ones and the sort are kept in the
browser. `recommended in` lists the `select` batches whose `best_result/`
chose the run. Enable Delete to tick runs and delete their folders, after
confirming; their InfluxDB buckets stay.

**charts** on a row opens the run: equity at each slippage with its drawdown
and the traded market's price, the distribution of trade PnL, holding time
against PnL, every `by_<grouping>.json` as bars and a table, and a weekday ×
hour heatmap. Time groups use each trade's open time in JST. A pnl0 / pnl5 /
pnl10 switch applies to the whole page. The slider under the drawdown is the
page's range: sliding it, scrolling on the equity chart or picking dates
narrows every tile, chart and grouping to the trades closed in it, recomputed
the way `summarize` computes the whole run's. The Summary panel stays
summary.json's.

The price is the market's last recorded trade per step, read through hase, the
step the finest that keeps a range to 5,000 points: four hours for a year, five
minutes for a week. A year takes about two seconds the first time; each day is
then kept in memory as minute prices.

**walk-forward** on a row opens the run against every rule of the newest
training of its group (`runs.json`, [03_walk_forward.md](03_walk_forward.md)):
per history and interval, switching's cumulative pnl5 and drawdown against
this run's set held over the rule's own test hours. The best rule by switching
pnl5 is outlined, and is the table's `wf best pnl5` and `wf best rule`.

**Training** lists each group trained by `walk-forward --train`: trained and
no-switching totals, their difference, the bootstrap p-value, retrainings won
and the rule to trade now. Its **charts** show the trained curve against no
switching and hindsight, the daily difference the bootstrap tests, every
candidate's score at each retraining, and the parameter set held over time.

## Links to a dashboard

A run recorded to InfluxDB can link to a dashboard that reads its bucket. The
viewer names none: `grafana_dashboard.json` in its env lists them, and a
strategy's dashboard and entry are kept with the strategy.

```json
{"dashboards": [{
  "match": {"trading.module_name": "example.strategy"},
  "url": "/grafana/d/example-backtest",
  "query": {"var-bucket": "{influx_bucket}", "var-symbol": "{symbol}",
            "from": "{start_ms}", "to": "{end_ms}"}
}]}
```

- `match`: keys of the flattened meta.json (`trading.module_name`,
  `config.market`, `params.x`) and the values they must equal.
- `{key}` in `url` and `query`: a flattened meta.json value, or `exchange`,
  `symbol` and `base` from `config.market` (`COINCHECK:BTC_SPOT` gives
  `COINCHECK`, `BTC_SPOT`, `BTC`), or `start_ms` and `end_ms`, the run's range
  in epoch milliseconds (`start_str` and `end_str` read as JST).

The first dashboard that matches, and has a value for every placeholder, gives
the run its link. A run without a bucket has no `{influx_bucket}`, so it gets
no link. With no dashboards there is no link column.

## The API

The page is `web/` (React), served from `web/dist`. Behind it:

| Endpoint | Returns |
|---|---|
| `GET /api/envs` | env names with a `run_log/` |
| `GET /api/results[?env=NAME]` | every finished run: meta.json, summary.json as `result`, `source`, `recommended_in`, `walk_forward`, `grafana_url` |
| `GET /api/runs/<env>/<run_id>?source=S` | a run's meta, summary, file list and `grafana_url` |
| `GET /api/runs/<env>/<run_id>/files/<name>?source=S` | `meta.json`, `summary.json`, `trades.csv` or a `by_*.json`, nothing else |
| `GET /api/runs/<env>/<run_id>/walk-forward?source=S` | the run against every rule of its group's newest training |
| `DELETE /api/results/<env>/<run_id>?source=S` | deletes the run folder |
| `GET /api/trainings[?env=NAME]` | every finished `post_analysis/train/<id>/` |
| `GET /api/trainings/<env>/<train_id>?source=S` | a training's meta, summary and file list |
| `GET /api/trainings/<env>/<train_id>/files/<name>?source=S` | one of the files `--train` writes |
| `GET /api/prices?market=EX:SYM&start=&end=` | the market's last trade price per step, epoch seconds in |

A folder is finished once it has `summary.json`; a `.tmp-*` folder is still
being written and is not listed. Envs are matched against the ones discovered,
never a path from the request; names with a dot and symlinks are refused.
Non-finite numbers come back as null. `source` is needed only when two envs
share a name.

## As a service

A systemd user unit, with the Python that has Enoshima installed:

```ini
[Unit]
Description=Enoshima results viewer
After=network.target

[Service]
ExecStart=/path/to/python -m enoshima.cli web
Restart=always

[Install]
WantedBy=default.target
```

It stops on SIGTERM and releases the env's lock. After a change to `web/`,
rebuild with `npm run build --prefix web`; after a change to either file in
`ENOSHIMA_API/`, restart it.
