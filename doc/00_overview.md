# Enoshima: the whole picture

Enoshima is a backtest engine for recorded crypto market data. It replays the
order books and trades already on disk through a strategy and a simulated
exchange, and writes what happened into the strategy's environment. This page
is the map; each part has its own page:

| Page | Covers |
|---|---|
| [01_weekly_run.md](01_weekly_run.md) | The cumulative weekly run: preload, overlap, which week a trade belongs to, how weeks are stored and stitched |
| [02_selection.md](02_selection.md) | `enoshima select`: filters, score, neighbour smoothing, groups, its walk-forward over months |
| [03_walk_forward.md](03_walk_forward.md) | `enoshima walk-forward`: testing how often to choose again, on how much history (X, Y), and `--train` |
| [04_web.md](04_web.md) | `enoshima web`: every run and training as a page, and links to a dashboard |

## What it is

A backtest is offline and deterministic because there is no other kind of run
available. [`qate`](https://github.com/kamakuraquantlab/qate) supplies the event
loop, the strategy base class and the simulator, and installs no networking
library at all. Enoshima builds a `BacktestGateway` per exchange and never
anything else. A strategy written for qate runs here unchanged. It reports
through qate's `Reporter` interface, as it does when it runs live.

Two rules break a backtest silently rather than loudly:

- **Time comes from the event.** `self.now_ts` is set from each event's
  timestamp. A strategy that calls `time.time()` still produces numbers, and
  those numbers compare a wall clock against replayed prices.
- **An order fills on the book *after* the one it was created on.** That is the
  simulator's latency model, and it stops a strategy trading on information it
  could not have had.

## Where the data comes from: hase

Each layer asks the one below it in its own vocabulary, and nothing skips a
layer:

    enoshima ──► hase.bronze ──► komachi.bronze
     replay       Market, Trade,   which file holds a market-day,
                  OrderBook        and whether it is complete

| | Owns | Knows nothing about |
|---|---|---|
| [`komachi`](https://github.com/kamakuraquantlab/Komachi) | The files: downloads them, places them in the data lake layout, says which file holds a market-day and whether it is complete | What is inside a file, and trading |
| [`hase`](https://github.com/kamakuraquantlab/Hase) | The data lake as trading data: markets as qate `Market`s, bronze as `Trade`s and `OrderBook`s | Replay, and where a run is configured |
| [`qate`](https://github.com/kamakuraquantlab/qate) | The model and the machinery: `OrderBook`, `Trade`, the event loop, the simulator | Storage layouts and where data lives |
| [`qate-env`](https://github.com/kamakuraquantlab/qate-env) | The run directory lifecycle and how a strategy's classes are resolved | Data, and replay |
| **Enoshima** | The replay: asks hase for `Trade`s and `OrderBook`s, tags them as events, runs them through qate | Files, paths, and komachi |

A request for "these books, these days" is
`hase.bronze.load_day(market, DataType.ORDER_BOOK, day)`. Enoshima has no
data-loading code of its own. If it had a second implementation of a question
hase already answers, the two would drift apart, and the day they disagreed a
backtest would silently skip a date the downloader thinks it has.

There is no path to configure. Komachi downloads the data, and its settings
file (`ROOT_PATH` in `~/.kamakuraquantlab.env`) is the only answer to where the
data is:

```bash
komachi download --market COINCHECK:BTC_SPOT --start 2026-09-01 --end 2026-09-07
komachi local     # what is on disk
```

Dates are Asia/Tokyo days, 15:00–14:59 UTC. The timestamps inside the files are
UTC epochs.

## The environment

Every command takes `--env NAME`. That names one directory under `~/env` (or
`QATE_ENV_ROOT`), and its files are the run's whole configuration. The command
takes no locations: the data is komachi's, and the results go back to the same
data lake.

| File | Holds |
|---|---|
| `strategy_config.json` | The strategy's `Config`: market and order size. The live runner reads the same file |
| `strategy_param_grid.json` | Strategy module and variant, each parameter's `default_value`, and the `values` a sweep tries |
| `enoshima.json` | How this env is backtested: fees, weekly preload and overlap, selection and walk-forward rules, memory. Optional |

```json
{
  "module_name": "my_strategy",
  "variant_name": "v1",
  "params": [
    {"name": "threshold", "default_value": 0.001, "values": [0.0005, 0.001]}
  ]
}
```

A single run uses every `default_value`. `--optimize grid` runs every
combination in `values`. The strategy is imported by module name, so it must be
installed or on `PYTHONPATH`. Its `Config` class is
`<module>.config.Config`, and there is no index file.

A run takes the environment's lock and logs there. Never point a run at an
environment a live process is running from; copy it instead. Every command logs
to `env.log` there and prints nothing. Its results go to the data lake, not to
the environment directory: see [Where the results are](#where-the-results-are).

### `enoshima.json`

Every key is optional. An unknown key is an error, so a misspelling cannot
silently change what a run does.

| Key | Is | Detail |
|---|---|---|
| `weekly` | `preload_days`, `overlap_days` (1 and 1) | [01_weekly_run.md](01_weekly_run.md) |
| `selection` | how `select` filters, scores, smooths and groups | [02_selection.md](02_selection.md) |
| `walk_forward` | the rules `walk-forward` tests, and `--train`'s settings | [03_walk_forward.md](03_walk_forward.md) |
| `fee_rates` | what each traded market charges, by market id; a traded market without one refuses the run | below |
| `chunk_days` | days loaded at once (2); `--chunk-days` overrides it | memory |
| `max_concurrency` | parameter sets sharing one pass over the data (16); `--max-concurrency` overrides it | speed |
| `strategy_log_level` | level of the strategy's and `qate.trading`'s loggers during a run (`WARNING`) | `"INFO"` to debug |

### Fee rates

qate costs a fill with its market's rate, which normally comes from the venue
package that also connects to the exchange. A backtest installs no such package,
so the env states the rates itself:

```json
"fee_rates": {"GMO:XRP_JPY": {"maker": 0.0, "taker": 0.0}}
```

A rate is a fraction of notional, and a negative maker rate is a rebate. Every
market the strategy trades (a market on one of the config's `get_exchanges()`)
needs a rate, zero written out, or the run is refused before anything is
replayed. A market read only for its data, such as a reference price on another
venue, needs none. Non-zero rates are part of the run's id. A zero rate is not,
so stating one keeps the folders of runs made before rates were stated.

## Two kinds of run

**Weekly** (`--weekly`) is the main use. The range is widened to whole ISO weeks,
Monday to Sunday JST, and each week is replayed on its own with a preload before
it and an overlap after it. Weeks already on disk are skipped, so extending a
year by a month replays only that month's weeks. The run's folder has no dates
in its id and grows with every run. Only weekly runs are candidates for `select`
and `walk-forward`. See [01_weekly_run.md](01_weekly_run.md).

```bash
enoshima run --env NAME --from 2026-01-01 --to 2026-12-31 --optimize grid --weekly
```

**Temporary** (no `--weekly`) is a quick look at some days: one continuous
replay of the range. It is kept and listed by `results`, with the range in
its id, and it is never a candidate for a recommendation.

```bash
enoshima run --env NAME --from 2026-09-01 --to 2026-09-01
```

Every run's id is a deterministic hash of the environment, the strategy and its
**revision** (the commit of the checkout the strategy is imported from,
`+dirty.<hash>` for uncommitted changes), the parameters, and the range (or,
weekly, the preload and overlap). A killed sweep therefore resumes where it
stopped, and a change to the strategy starts a new folder rather than mixing
two versions' results. `--force` reruns a set that already has a result.

Every run is evaluated at 0, 0.005% and 0.01% extra slippage, reported as
`pnl0`, `pnl5` and `pnl10`. An edge that survives at zero and vanishes at 0.005%
has told you something, so nothing is ranked on `pnl0`. The slippage is charged
at the close, on both legs.

## Where the results are

In the data lake, beside the bronze the run replayed: komachi's root, under
the `research/` layer. Nothing but `env.log` goes to the environment directory.

```text
<lake>/research/
├── dataset=BacktestRun/env=<env>/run_id=<id>/            one row: the run's meta, summary and groupings;
│                                                         "finished" means this exists
├── dataset=BacktestPart/env=<env>/run_id=<id>/part=<p>/  one row per part: its meta and summary
├── dataset=BacktestTrade/...  same partitions            every closed position, with its PnL at each
│                                                         slippage and its closing fill's price
├── dataset=BacktestOrder/...                             every fill
├── dataset=BacktestPnl/...                               every PnL update, open and close, with open_ts
├── dataset=BacktestBar/...                               the strategy's completed bars and indicators
├── dataset=BacktestMetric/...                            any other metric it reports, and EVAL
└── enoshima/<env>/
    ├── best_result/<batch_id>/                           `select`'s recommendations (02)
    └── post_analysis/                                    `walk-forward`'s table and `--train` folders (03)
```

`run_id` is the deterministic hash of the env, the strategy and variant, the
revision of the variant's source, the parameters, the non-zero fee rates and
the range (weekly: the preload and overlap instead), so every file of every
dataset is attributable to exactly what computed it, and a re-run replaces
rather than adds. `BacktestRun` holds what the hash was made of --
`strategy`, `strategy_rev`, `param_set_id`, `params`, ... -- as columns, and
every summary figure as a column too, so the lake answers a question across
runs directly:

```sql
SELECT run_id, param_set_id, pnl5, max_drawdown5
FROM read_parquet('<lake>/research/dataset=BacktestRun/**/*.parquet',
                  hive_partitioning = true, hive_types_autocast = false, union_by_name = true)
WHERE env = 'EXAMPLE_BACKTEST' ORDER BY pnl5 DESC
```

(`hive_types_autocast = false`: a run id of sixteen digits is still a string.)

`part` is what a run writes at once: an ISO week of a weekly run
([01_weekly_run.md](01_weekly_run.md)), `all` for a temporary run. A part is
written whole, its data files first and its `BacktestPart` row last, and the
run's row is rebuilt from its parts after; a reader refuses a part one of
whose files has gone. Any window or grouping can be recomputed from the trades
without a replay, which is what `select` and `walk-forward` do. A grouping row
has `trades` and, per slippage N, `winsN`, `pnlN`, `peakN`, `troughN` and `ddN`
(worst drawdown). Nothing from the warmup is recorded, and a weekly part's
orders and PnL are those of the positions it opened (01).

`enoshima results --env NAME` lists the recorded runs, best first, in `env.log`;
`enoshima web` shows every env's as a page ([04_web.md](04_web.md)). No command
deletes results, and the viewer deletes only a run it is asked to. To clear an
env's, delete its `env=<env>` folder in each dataset and its `enoshima/<env>/`.

A run is written to the lake only after it has finished: a run that fails
writes nothing. InfluxDB is not written; a loader from the lake into InfluxDB
for dashboards is a separate tool.

### Results from before the lake

Results used to be written into the environment, as `run_log/`, `best_result/`
and `post_analysis/`. `enoshima import-run-log --env NAME` reports what it
would bring into the lake, and `--apply` does it: each run folder's weeks (or
its root, for a temporary run) become parts with their meta, summary and
trades, keeping their run ids, so a weekly run carries on from them. Their
orders and PnL updates were only in InfluxDB, so imported parts have none.
Nothing in the environment is removed; delete the folders once the import
looks right. `enoshima.json`'s old `"influx"` entry is ignored, with a warning.

## Choosing a parameter set

A run does not choose. `enoshima select` reads every weekly run and
recommends a parameter set per range and per group. It filters and scores on
several figures, smooths over grid neighbours, and tests its choice month by
month out of sample ([02_selection.md](02_selection.md)).
`enoshima walk-forward` tests the next question with the same ranking: whether
choosing again every Y on the last X of history beats holding one set
([03_walk_forward.md](03_walk_forward.md)).
