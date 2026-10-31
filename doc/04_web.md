# The results viewer: `enoshima web`

```bash
pip install -e '.[web]'                 # Flask; the engine itself needs none
npm install --prefix web && npm run build --prefix web
enoshima web --setup                    # once: the viewer's own env, ENOSHIMA_API
enoshima web                            # http://localhost:9768
```

Every run and walk-forward training the other commands wrote to the data
lake, as one page: a table of all runs, a chart page per run, and the
trainings. Like `select` and `walk-forward`, it replays nothing; it reads the
lake ([00_overview.md](00_overview.md#where-the-results-are)) at komachi's
root, as every command does. It launches nothing either: there is no endpoint
that runs a backtest, changes a parameter or writes a config. The one thing it
changes is a run it is asked to delete. The module docstring (`enoshima.web`) is the reference.

## Its env, `ENOSHIMA_API`

The viewer runs in an env of its own under qate-env's root, like every other
command, so its log, its lock (one viewer per root) and its configuration sit
in one folder. `--setup` writes:

```text
ENOSHIMA_API/
├── config.json              {"port": 9768}
├── grafana_dashboard.json   {"dashboards": []}
└── env.log                  once it has run
```

An env is listed when the lake has a finished run of it, wherever its
definition lives. A `results_dirs` left in `config.json` from when the viewer
read env folders is ignored. Both files are read once at start.

## What it shows

**The table** is every finished run, newest first: its meta flattened into
columns, and its summary as `result.*`. Envs are tabs; a keyword search
checks every field; clicking a batch id filters by it; headers sort on several
columns; × hides a column, and the hidden ones and the sort are kept in the
browser. `recommended in` lists the `select` batches whose `best_result/`
chose the run. Enable Delete to tick runs and delete their files in the lake,
after confirming.

The dates beside the filter summarize every run over those JST days instead:
each `result.*` is `summarize` of the run's trades that closed in them, the
way the run page narrows to a range, and `range covered` says whether the
run's own range holds every day of it -- a run that does not is summarized
over fewer days than its neighbours. The per-order slippage averages are
blank there, as a trade row does not say which orders made it. The first range
of an env reads every run's trades, about a tenth of a second a run; later
ones are kept in memory.

**charts** on a row opens the run: equity at each slippage with its drawdown
and the traded market's price, the distribution of trade PnL, holding time
against PnL, every grouping (`by_<grouping>.json`) as bars and a table, and a weekday ×
hour heatmap. Time groups use each trade's open time in JST. A pnl0 / pnl5 /
pnl10 switch applies to the whole page. The slider under the drawdown is the
page's range: sliding it, scrolling on the equity chart or picking dates
narrows every tile, chart and grouping to the trades closed in it, recomputed
the way `summarize` computes the whole run's. The Summary panel stays the
run's summary.

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

A run can link to a dashboard of its own -- one reading what a loader put in
InfluxDB from the lake, say. The viewer names none: `grafana_dashboard.json`
in its env lists them, and a strategy's dashboard and entry are kept with the
strategy.

```json
{"dashboards": [{
  "match": {"trading.module_name": "example.strategy"},
  "url": "/grafana/d/example-backtest",
  "query": {"var-run": "{id}", "var-symbol": "{symbol}",
            "from": "{start_ms}", "to": "{end_ms}"}
}]}
```

- `match`: keys of the flattened meta (`trading.module_name`,
  `config.market`, `params.x`) and the values they must equal.
- `{key}` in `url` and `query`: a flattened meta value, or `exchange`,
  `symbol` and `base` from `config.market` (`COINCHECK:BTC_SPOT` gives
  `COINCHECK`, `BTC_SPOT`, `BTC`), or `start_ms` and `end_ms`, the run's range
  in epoch milliseconds (`start_str` and `end_str` read as JST).

The first dashboard that matches, and has a value for every placeholder, gives
the run its link. Runs written to the lake have no `influx_bucket`, so a
dashboard asking for `{influx_bucket}` links only those imported from before
the lake. With no dashboards there is no link column.

## The API

The page is `web/` (React), served from `web/dist`. Behind it:

| Endpoint | Returns |
|---|---|
| `GET /api/envs` | env names with a finished run in the lake |
| `GET /api/results[?env=NAME]` | every finished run: its meta, its summary as `result`, `source` (the env), `recommended_in`, `walk_forward`, `grafana_url` |
| `GET /api/results?env=NAME&start=YYYY-MM-DD&end=YYYY-MM-DD` | the same, each `result` over the trades closed in those JST days (either may be left out), and `range_covered` |
| `GET /api/runs/<env>/<run_id>?source=S` | a run's meta, summary, file list and `grafana_url` |
| `GET /api/runs/<env>/<run_id>/files/<name>?source=S` | `meta.json`, `summary.json`, `trades.csv`, a `by_*.json`, or the records as `orders.csv`, `pnl.csv`, `bars.csv`, `metrics.csv`; nothing else |
| `GET /api/runs/<env>/<run_id>/walk-forward?source=S` | the run against every rule of its group's newest training |
| `DELETE /api/results/<env>/<run_id>?source=S` | deletes the run's files, in every dataset |
| `GET /api/trainings[?env=NAME]` | every finished `post_analysis/train/<id>/` |
| `GET /api/trainings/<env>/<train_id>?source=S` | a training's meta, summary and file list |
| `GET /api/trainings/<env>/<train_id>/files/<name>?source=S` | one of the files `--train` writes |
| `GET /api/prices?market=EX:SYM&start=&end=` | the market's last trade price per step, epoch seconds in |

A run is finished once it has its `BacktestRun` row; a training folder once it
has `summary.json`, and a `.tmp-*` one is still being written and is not
listed. Envs are matched against the ones discovered, never a path from the
request; ids with a dot and symlinks are refused. Non-finite numbers come back
as null. `source` is the env's name; the page sends it back, and it is not
needed.

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
