# Enoshima

A backtest engine for recorded crypto market data. It replays the order books and
trades you already have on disk through a strategy and a simulated exchange, and
writes what happened next to them.

It cannot reach an exchange. That is a property of what it is built on:
[`qate`](https://github.com/kamakuraquantlab/qate) supplies the event loop, the
strategy base class and the simulator, and installs no networking library at all —
not an HTTP client, not a WebSocket client. A backtest here is offline and
deterministic because there is no other kind of run available.

```bash
pip install kamakuraquantlab-enoshima
```

Named for the island off the Shonan coast, the far end of the line. Komachi is
where you get the data, Hase is where you look at it, Enoshima is where you find
out whether an idea would have worked.

## How the three fit together

Each layer asks the one below it in its own vocabulary, and nothing skips a layer:

| | Owns | Knows nothing about |
|---|---|---|
| [`komachi`](https://github.com/kamakuraquantlab/Komachi) | The files. It downloads them, places them in the data lake layout, and answers which file holds a market-day and whether it is complete | What is inside a file, and trading |
| [`hase`](https://github.com/kamakuraquantlab/Hase) | The data lake as trading data: markets as qate `Market`s, a range of bronze as `Trade`s and `OrderBook`s, and silver and gold derived from it | Replay, and where a run is configured |
| [`qate`](https://github.com/kamakuraquantlab/qate) | The model and the machinery: `OrderBook`, `Trade`, the event loop, the simulator | Storage layouts, where data lives, and where a run is configured |
| [`qate-env`](https://github.com/kamakuraquantlab/qate-env) | The run directory: the strategy an environment names, its config and params, and the lock | Data, and replay |
| **Enoshima** | The replay: asks hase for `Trade`s and `OrderBook`s, tags them as events, runs them through qate | Files, paths, and komachi |

`qate-env` is the one a live runner shares, so a backtest environment holds the
same files a live one does, `params.json` included: backtesting what production
runs is copying its definition into a backtest env. A run writes into its own
environment and takes its lock, so it is never pointed at a live one.

A backtest's request for "these books, these days" is
`hase.bronze.load_day(market, DataType.ORDER_BOOK, day)`; hase turns it into
`komachi.bronze.file(...)`, opens what it is handed, and converts. Enoshima holds
no data-loading code of its own, because two implementations of one question
drift — and the day they disagree is the day a backtest silently skips a date
the downloader thinks it has.

## Where the data comes from

[Komachi](https://github.com/kamakuraquantlab/Komachi) downloads it, and Enoshima
reads it where Komachi put it — no path to configure:

```bash
komachi download --market COINCHECK:BTC_SPOT --start 2026-09-01 --end 2026-09-07
komachi local
```

`komachi local` is what is on disk. Komachi is the only answer to where that is:
`ROOT_PATH` in its settings file, `~/.kamakuraquantlab.env`. To replay a
different tree in the same Hive-partitioned layout, change it there. Dates are
Asia/Tokyo days spanning 15:00–14:59 UTC; the timestamps inside the files are UTC
epochs.

## Quick start

The repository ships a runnable example: a moving-average crossover and an
environment that backtests it.

```bash
git clone https://github.com/kamakuraquantlab/Enoshima && cd Enoshima
pip install -e .

export QATE_ENV_ROOT=$PWD/example/env
export PYTHONPATH=$PWD   # so `example.strategy` can be imported
enoshima run --env EXAMPLE_BACKTEST --from 2026-09-01 --to 2026-09-01
```

`trading.json` names the strategy as a module, and Enoshima imports it by that
name, so a strategy of your own must be importable the same way: installed into
the environment, or on `PYTHONPATH`.

Nothing is printed: every command logs to `env.log` in the environment directory,
and that is where to watch a run.

```bash
tail -f example/env/EXAMPLE_BACKTEST/env.log
```

```text
... INFO     enoshima.runner START env=EXAMPLE_BACKTEST strategy=example.strategy.v1 range=2026-09-01 00:00:00..2026-09-01 23:59:59 mode=single combos=1
... INFO     enoshima.data_source Loaded 2026-08-31..2026-09-01: 269463 events
... INFO     enoshima.runner RESULT params=(fast_period=20 slow_period=120) pnl5=-7474.41 trades=1292 sharpe=-261.631
```

It loses money, and it is supposed to: a crossover that crosses 1,292 times in a
day pays the spread 1,292 times. That is the first thing a backtest is for.

Then sweep the grid in `param_grid.json`:

```bash
enoshima run --env EXAMPLE_BACKTEST --from 2026-09-01 --to 2026-09-01 --optimize grid
enoshima results --env EXAMPLE_BACKTEST
```

`results` writes its table to `env.log` too:

```text
        pnl5  trades   sharpe  range                                     params
    -4005.76     671 -139.398  2026-09-01 00:00:00..2026-09-01 23:59:59  fast_period=40 slow_period=240
    -9621.36    1632 -339.923  2026-09-01 00:00:00..2026-09-01 23:59:59  fast_period=10 slow_period=120
```

## Writing a strategy

Copy `example/strategy/`. Two files, and the contract is small:

- **`config.py`** — a `StrategyConfig` saying which markets and event types to replay.
  This is what makes a run *a different run*: a market, an order size.
- **`v1.py`** — exactly one class named `Variant`, constructed with
  `(config, params)`. `params` is what an optimize sweep varies.

```python
class Variant(Strategy[Config]):
    def warmup_order_book(self, order_book): ...   # fill history, do not trade
    def handle_order_book(self, order_book): ...   # decide, place orders
    def handle_order_filled(self, order_response): ...  # publish the PnL
```

Two rules break a backtest silently rather than loudly:

- **Time comes from the event.** `self.now_ts` is set from each event's timestamp.
  A strategy that calls `time.time()` still runs and still produces numbers, and
  those numbers compare a wall clock against replayed prices.
- **An order fills on the book *after* the one it was created on.** That is the
  simulator's latency model, and it is what stops a strategy trading on
  information it could not have had.

Point an environment at your strategy with `trading.json`:

```json
{ "strategy_module_name": "my_strategy", "variant": "v1" }
```

## An environment

One directory per named run configuration, under `~/env` (or `QATE_ENV_ROOT`):

| File | Holds |
|---|---|
| `trading.json` | Which strategy module and variant to run |
| `config.json` | The strategy's `Config` — market, order size |
| `params.json` | **The parameters one run uses.** The same file a live run reads |
| `param_grid.json` | The search space `--optimize grid` walks. Optional |
| `params_combo.json` | An explicit parameter list, for `--optimize combo`. Optional |
| `features.json` | Feature flags. Optional |
| `enoshima.json` | How this env is backtested: InfluxDB recording, memory. Optional |

There is no index file. Each file is read by whatever wants it, with the class
named by the reader — so an environment written when a strategy lived somewhere
else still loads.

**`params.json` is what a single run uses**, so backtesting exactly what a live
process is running is one command rather than a comparison of two files. A grid is
a search space — an input to `--optimize` — not a description of the environment.
An env whose `params.json` is empty falls back to the grid's defaults.

The strategy's `Config` class is resolved from the strategy module
(`<strategy_module>.config.Config`).

A run writes back into the same directory:

```text
EXAMPLE_BACKTEST/
├── env.log                            everything a command logged
├── run_log/<id>/                      one per parameter set and range
│   ├── meta.json                      what was run: env, config, params, range, bucket
│   ├── summary.json                   the whole-range result at each slippage rate
│   ├── trades.csv                     every closed position, with its PnL change at each rate
│   └── by_<grouping>.json             the trades grouped: month, week, day, weekday,
│                                      monthday, hour, weekday_hour, holding, side
└── best_result/<batch_id>/            a sweep's choice
    ├── selection.json                 the ranking, every fold, the out-of-sample totals
    └── <id>.json                      the chosen run's meta.json (absent if none passed)
```

A grouping's row holds `trades`, and per slippage suffix N `winsN`, `pnlN`, and
the `peakN`, `troughN` and `ddN` (worst drawdown) of the group's own PnL from
zero. For month, week and day that is the period's equity curve; for weekday,
hour, holding time and side it is the curve of a strategy that traded only in
that group.

A run's PnL is kept on disk because the result is evaluated from it. Orders,
PnL and the strategy's bars go to InfluxDB when the environment asks for it —
see below — for charting.

## `enoshima.json`

How an environment is backtested, as opposed to what it trades. Every key is
optional, and a missing file means the defaults:

```json
{
  "influx": {
    "token": "",
    "write_url": "http://localhost:8086",
    "read_url": "http://localhost:8086",
    "org": null
  },
  "chunk_days": 2,
  "max_concurrency": 16,
  "strategy_log_level": "WARNING",
  "selection": {
    "positive_pnl10": true,
    "min_trades": 0,
    "max_drawdown": null,
    "score": "balanced",
    "top_share": 0.05,
    "neighbors": true,
    "window": "expanding",
    "train_months": 6,
    "test_months": 1
  }
}
```

`chunk_days` and `max_concurrency` size a run (see below); `--chunk-days` and
`--max-concurrency` override them for one run. `strategy_log_level` is the level
of the strategy's loggers and `qate.trading`'s during a backtest: a live run logs
every fill at INFO, which a sweep would repeat for every fill of every parameter
set, so the default is `WARNING`. Set `"INFO"` to see them while debugging. An unknown key is an error, so a
misspelling cannot silently change what a run does.

## Charting a run in InfluxDB

A run's result -- the evaluation in its run log -- needs nothing but the data.
To also chart it in Grafana, give the environment an `"influx"` entry in
`enoshima.json`, and every run of it records its metrics, orders and PnL as it
goes:

```bash
echo '{"influx": {}}' > "$QATE_ENV_ROOT/EXAMPLE_BACKTEST/enoshima.json"
enoshima run --env EXAMPLE_BACKTEST --from 2026-09-01
```

One bucket per parameter set and run, `backtest_<param_set_id>_<batch_id>`, so a
sweep's combinations sit side by side under one batch id; the run's `meta.json`
records which. The id is built by
Enoshima from `param_grid.json`, not by the strategy: the environment's name, then
the parameter values as plain decimals in the grid's order. In
`EXAMPLE_BACKTEST`, `fast_period=20, slow_period=120` is `EXAMPLE_BACKTEST_20_120`,
and `threshold=0.0015` would be `0.0015`. Parameters the
grid does not name follow. An environment's market,
strategy and variant are fixed, so its name stands for them, and the same values
always give the same id. The batch id makes every execution's bucket its own: a
`--force` re-run does not write on top of the run it repeats.
Enoshima never expires or deletes a bucket; cleaning up is left to whatever
manages your InfluxDB. Connection details are
the entry's own: `"token"`, `"write_url"` and `"read_url"` default to a local
server at `http://localhost:8086` with no token, and `"org"` defaults to the only
organisation the token sees. The token sits in `enoshima.json` rather than among
the credentials because the InfluxDB is your own, not an outside service.

It goes straight to the database. A live process writes a local log first and
ships it later, because trading must never wait on a server; a backtest has no
such constraint, so it skips the intermediate file.

This is `qate`'s `Reporter` interface, the same one a live run uses:
`BacktestRuntime` hands every fill, metric and PnL update to its reporters, and
`InfluxReporter` is one of them.

## Every run is evaluated three times

At 0, 0.005% and 0.01% extra slippage, reported with the suffixes `0`, `5` and
`10` — `pnl0`, `pnl5`, `pnl10`. One number is not a result. A strategy whose edge
survives at zero slippage and vanishes at 0.005% has told you something important
about itself, and reporting only the optimistic figure hides it. `--optimize`
weighs `pnl5` and `pnl10`, never `pnl0`, for the same reason.

## How `--optimize` chooses

The best whole-range PnL is the set that fit that range best, which is not the
set most likely to keep working. A sweep therefore chooses in four steps, all
from the run folders it has already written:

1. **Filters.** A set that loses money at `pnl10` ranks below every set that
   does not; `min_trades` and `max_drawdown` (on pnl5) can be added.
2. **Score.** With `"score": "balanced"`, four figures: `pnl5` (realistic),
   `pnl10` (degraded execution), `dd5`, the maximum drawdown at pnl5 (a
   period that does not suit it), and `top_share5`, the share of its winning
   pnl5 made by its best `top_share` of trades (whether a few big wins carry
   it). Each becomes a percentile among the sets that pass -- 1 for the best,
   0 for the worst -- and the score is their mean, unweighted. `"pnl5"` ranks
   on pnl5 alone.
3. **Neighbours.** Each figure is averaged with the same figure of its
   neighbours on the grid -- the sets one step away along one numeric
   parameter -- before it is ranked, so a lone spike loses to a plateau.
   Categorical parameters have no neighbours.
4. **Walk-forward.** Each run folder has its PnL by JST month (`by_month.json`),
   and its trades. The sweep chooses on the months before a test month, scores
   that choice on the following `test_months`, steps forward and repeats. The
   sum of those test months is what choosing this way would have earned, out
   of sample -- the figure to trust. With `"window": "expanding"` a choice is
   made on every complete month since the start, and `train_months` is only the
   history the first test needs; the set recommended is chosen on the whole
   range. `"rolling"` uses the latest `train_months` instead, and recommends
   on the most recent ones.

`best_result/<batch_id>/selection.json` holds the ranking, every fold and the
out-of-sample totals, and `<id>.json` beside it is the recommended run's
`meta.json`. When no set passes the filters there is no copy: nothing is
recommended.

## Commands

| Command | Does |
|---|---|
| `enoshima run` | Run a backtest, or a parameter sweep |
| `enoshima results` | Recorded runs for an environment, best first |

To clear an environment's results, delete its `run_log/` and `best_result/`.

Flags on `run`: `--optimize grid|combo`, `--chunk-days` (memory),
`--max-concurrency` (parameter sets per data pass), `--force` (re-run a parameter
set that already has a result). The variant is `trading.json`'s, the data is
komachi's, and the output is the environment directory.

## Memory, and how a sweep is cheap

A day of full-depth order books is hundreds of megabytes once it is Python
objects, so the range is loaded a couple of days at a time into **one** buffer
that every parameter set in a batch reads from. A sweep therefore costs
`ceil(combos / --max-concurrency)` passes over the data, not one pass per
combination — five parameter sets over a day of real books take about as long as
one. The next chunk is loaded while the current one is replayed, so up to two
chunks are in memory at once.

If a run is killed by the OOM reaper, lower `chunk_days` before anything else.

Runs are also resumable. A run log's id is a hash of the environment, strategy,
parameters and date range, so a sweep that dies part way through resumes where it
stopped, and the combinations it skips are still ranked when it picks a best.

## Running on free-threaded Python

A batch's parameter sets are threads. On an ordinary build they take turns on
one core; on a free-threaded build (3.14t) they run at once, and a sweep is
bounded by cores rather than by the GIL. `run.sh` runs `enoshima` that way and
refuses to start on anything else:

```bash
pyenv virtualenv 3.14.0t enoshima-314t
pip install --only-binary=:all: 'pandas>=3.0' numpy pyarrow dacite influxdb-client httpx matplotlib
pip install --no-deps -e . -e ../qate -e ../qate-env -e ../Hase -e ../Komachi -e <your strategy>
./run.sh run --env MY_ENV --from 2026-06-01 --to 2026-06-30 --optimize grid
```

`pandas>=3.0` is the part that matters: earlier versions re-enable the GIL on
import, and `run.sh` checks for exactly that. On a free-threaded build, set
`max_concurrency` to the size of the grid if memory allows: one pass loads the
data once, and the threads no longer cost each other a core.

## Development

```bash
pip install -e '.[dev]'
pytest
```

`tests/test_run_end_to_end.py` drives a real run from synthetic parquet to
results on disk, which is the test to read first and to keep passing.

## Licence

Apache 2.0. See [LICENSE.md](LICENSE.md).
