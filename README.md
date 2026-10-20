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
| [`qate-env`](https://github.com/kamakuraquantlab/qate-env) | The run directory lifecycle and strategy class-resolution convention | Data, and replay |
| **Enoshima** | The replay: asks hase for `Trade`s and `OrderBook`s, tags them as events, runs them through qate | Files, paths, and komachi |

The live runner and Enoshima share `strategy_config.json`; each owns the other
files its job requires. A run writes into its own environment and takes its lock.

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

`strategy_param_grid.json` names the strategy as a module, and Enoshima imports it by that
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

A run without `--weekly` like this one is a quick look: kept, listed by
`results`, and never a candidate for `select`. The main use is weekly
([below](#extending-a-backtest-week-by-week)): sweep the grid in
`strategy_param_grid.json` over whole weeks, then choose when you ask to:

```bash
enoshima run --env EXAMPLE_BACKTEST --from 2026-09-01 --to 2026-09-30 --optimize grid --weekly
enoshima results --env EXAMPLE_BACKTEST
enoshima select --env EXAMPLE_BACKTEST
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

Point an environment at your strategy and define its parameters with
`strategy_param_grid.json`:

```json
{
  "module_name": "my_strategy",
  "variant_name": "v1",
  "params": [
    {"name": "threshold", "default_value": 0.001, "values": [0.0005, 0.001]}
  ]
}
```

## Extending a backtest week by week

This is how Enoshima is mainly used. A backtest of a year, re-run a month
later over thirteen months, should not replay the year again. `--weekly`
makes that possible:

```bash
enoshima run --env EXAMPLE_BACKTEST --from 2026-01-01 --to 2026-12-31 --weekly
enoshima run --env EXAMPLE_BACKTEST --from 2026-01-01 --to 2027-01-31 --weekly   # replays only January's weeks
```

The range is widened to the ISO weeks that cover it, Monday 00:00 to Sunday
24:00 JST, and each week is replayed on its own: a fresh strategy and simulated
exchange, fed `weekly.preload_hours` (24 by default) of data before the Monday,
reporting from the Monday on. Asked for 2026-10-08, it replays 10-04 to 10-11
and reports 10-05 to 10-11. A week's result depends only on the environment,
the parameters, the preload and the week, so a week already on disk is skipped,
and a window's result is its weeks' trades put together -- identical, to the
byte, whichever run computed each week.

It runs as far as there is data. Each week is clipped to the complete days on
disk -- the first to the last day that every stream the strategy reads has --
so a range asked for up to today runs the current week to yesterday, and a
week with no such day is not run. A partial week records its own
`start_str`/`end_str` and `"partial": true`, and the next weekly run whose data
reaches further into it runs it again, so the last week grows to full without
`--force`. A day missing between the first and the last is not clipped: it
replays as empty, with a warning. `--force` reruns the weeks asked for.

What it trades away is equality with one continuous replay. Each week starts
cold: a position opened before the preload and still open on the Monday is in
neither week, and a strategy whose state needs more history than the preload
behaves differently on Mondays. Make the preload longer than the strategy's
memory. Each week's `summary.json` has `open_at_end`, the positions its replay
ended holding.

The run is cumulative. Its folder is `run_log/<id>/`, with an id that has no
dates in it -- the environment, the strategy and its revision, the parameters
and the preload -- and every weekly run of them adds its weeks to
`weeks/<YYYY-Www>/` (`meta.json`, `summary.json`, `trades.csv` per week). The
root `meta.json`, `summary.json`, `trades.csv` and `by_*.json` are never the
current run's: they are rebuilt from every week on disk, from the first
week's start to the last week's end -- a partial week's own -- so `select`, `walk-forward` and
`results` read a weekly run like any other. To keep that range true, a run
also replays the weeks between those on disk and those asked for: weeks 1-10
on disk and week 30 asked for replays 11-30. A root whose weeks have a gap --
a week that failed -- has no `summary.json` until a run fills it.

## The strategy's revision is part of every run's id

A run's numbers come from the strategy's code as much as from its parameters,
so the id of every run, weekly or not, includes the commit of the git checkout
the strategy module is imported from -- the strategy's repository, not
Enoshima's. A commit gives a new folder: a weekly run never puts weeks of two
versions together, and `select` and `walk-forward` rank runs of one revision
at a time (`module.variant@rev`). Uncommitted changes are a revision of their
own, `<sha>+dirty.<hash>`, from the diff and the untracked `.py` files;
reverting them returns to the clean revision's folder. A strategy installed
rather than checked out is its package's version. `meta.json` records it as
`strategy_rev`.

## An environment

One directory per named run configuration, under `~/env` (or `QATE_ENV_ROOT`):

| File | Holds |
|---|---|
| `strategy_config.json` | The strategy's `Config` — market, order size |
| `strategy_param_grid.json` | Strategy module and variant, parameter defaults, and sweep values |
| `enoshima.json` | How this env is backtested: InfluxDB recording, memory. Optional |

There is no index file. Each file is read by whatever wants it, with the class
named by the reader — so an environment written when a strategy lived somewhere
else still loads.

One run uses every parameter's `default_value`. `--optimize grid` expands every
combination in `values`. There is no second parameter file that can disagree.

The strategy's `Config` class is resolved from the strategy module
(`<strategy_module>.config.Config`).

A run writes back into the same directory:

```text
EXAMPLE_BACKTEST/
├── env.log                            everything a command logged
├── run_log/<id>/                      one per strategy revision, parameter set and range (or,
│                                      --weekly, preload: the root rebuilt from weeks/<YYYY-Www>/)
│   ├── meta.json                      what was run: env, config, params, range, bucket
│   ├── summary.json                   the whole-range result at each slippage rate
│   ├── trades.csv                     every closed position, with its PnL change at each rate
│   └── by_<grouping>.json             the trades grouped: month, week, day, weekday,
│                                      monthday, hour, weekday_hour, holding, side
├── best_result/<batch_id>/            `select`'s recommendations
│   ├── selection.json                 per range and group: ranking, folds, out-of-sample totals
│   └── <id>.json                      each recommended run's meta.json
└── post_analysis/
    ├── walk_forward_<stamp>.json      `walk-forward`'s rule table (and .txt)
    └── train/<id>/                    one `walk-forward --train` per range and group
        ├── meta.json                  what was trained: range, group, rules, baseline, settings
        ├── folds.json                 each retraining: windows, every candidate's ranking, the choice
        ├── switches.json              which rule and parameter set the trained curve held, and when
        ├── curve.csv                  PnL closed per JST hour: trained, baseline, hindsight, each rule
        └── summary.json               totals, significance, the rule to trade now (written last)
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
    "group_by": null,
    "train_months": 6,
    "test_months": 1
  },
  "walk_forward": {
    "window": ["expanding"],
    "history": {"6m": ["1m", "never"]},
    "test_from": null,
    "train_window": "expanding",
    "train_history": "1m",
    "retrain": "1m",
    "baseline": "default",
    "block_days": 5,
    "resamples": 10000
  },
  "weekly": {
    "preload_hours": 24
  }
}
```

`weekly` is how a `--weekly` run replays a week; see [Extending a backtest week
by week](#extending-a-backtest-week-by-week). The preload is part of the run's
id: a different one starts a different set of weeks.

`walk_forward` is what `enoshima walk-forward` tries; see [Testing the rule
itself](#testing-the-rule-itself).

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
Enoshima from `strategy_param_grid.json`, not by the strategy: the environment's name, then
the parameter values as plain decimals in the grid's order. In
`EXAMPLE_BACKTEST`, `fast_period=20, slow_period=120` is `EXAMPLE_BACKTEST_20_120`,
and `threshold=0.0015` would be `0.0015`. Parameters the
grid does not name follow. An environment's market,
strategy and variant are fixed, so its name stands for them, and the same values
always give the same id. The batch id makes every execution's bucket its own: a
`--force` re-run does not write on top of the run it repeats.
Enoshima never expires or deletes a bucket; cleaning up is left to whatever
manages your InfluxDB. A `--weekly` run is the exception to one bucket per
execution: it has one, `backtest_<param_set_id>_w<run_id>`, every point tagged
`week=<YYYY-Www>`, and a week's points are deleted by that tag before the week
is written again, so the bucket holds the weeks on disk, each once. Connection details are
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

The extra slippage is charged when a position closes, on both legs -- the open
leg on its cost -- as qate defers the open's fee to the close. A trade then
carries its whole cost even when its open was replayed before the range.

## How `enoshima select` chooses

A run writes results and summaries; it does not choose. `enoshima select`
recommends from the weekly run folders on disk when asked -- after a sweep,
after extending it, after changing the rule -- replaying nothing, and
`enoshima walk-forward` tests the rule itself, also only when asked.

The best whole-range PnL is the set that fit that range best, which is not the
set most likely to keep working. `select` therefore chooses in four steps, all
from the run folders already written:

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
   and its trades. `select` chooses on the months before a test month, scores
   that choice on the following `test_months`, steps forward and repeats. The
   sum of those test months is what choosing this way would have earned, out
   of sample -- the figure to trust. With `"window": "expanding"` a choice is
   made on every complete month since the start, and `train_months` is only the
   history the first test needs; the set recommended is chosen on the whole
   range. `"rolling"` uses the latest `train_months` instead, and recommends
   on the most recent ones.

This is done once per **group**, not once per sweep. Every finished run folder
in the environment is read and grouped by strategy, its revision and range, then by `group_by`: the categorical parameters by default
(a schedule, a mode), since each is a different way of trading rather
than a point on a scale. Each group is ranked on its own, with neighbours from its
own runs, and gets its own recommendation; `"group_by": []` makes a range one
group. Temporary runs -- any run without `--weekly` -- are not candidates.

`best_result/<batch_id>/selection.json` holds every group's ranking, folds and
out-of-sample totals, and each recommended run's `meta.json` is copied beside it
as `<id>.json`. A group where no set passes the filters has no copy: nothing is
recommended for it.

## Testing the rule itself

The walk-forward above has parameters of its own -- the window, how much
history, how often it chooses again -- and `enoshima walk-forward` tests them,
from the same run folders, to the hour. The rules are `enoshima.json`'s
`"walk_forward"`: each `window` with each `history` and that history's own
reselection intervals -- `"history": {"3d": ["4h", "1d"], "2m": ["1w", "1m"]}` --
since an interval that suits three days of history is noise on two months.

```bash
enoshima walk-forward --env NAME --sum A+B
```

Spans are hours, days, weeks or calendar months (`4h`, `1d`, `2w`, `6m`);
`never` chooses once and holds the set to the end. Each fold makes the choice
`selection` makes, with the env's filters, score, neighbours and `group_by`, on
the training hours, then trades it until the next choice; a trade belongs to
the hour it closed in. Every rule is tested on the same hours -- from
`test_from`, by default the range start plus the longest history -- so their
totals compare, and `hindsight`, the set chosen on the whole range, is listed
beside them as the ceiling it is. `--sum A+B` adds groups traded side by side,
such as two schedules that split the day. The table goes to `env.log` and
`post_analysis/`.

Trying many rules on the same test days is itself a choice made in sample:
prefer the rule that holds up across groups and environments over the one with
the best total in one.

### Training the rule

```bash
enoshima walk-forward --env NAME --train
```

`--train` makes that choice the way a deployment would, and tests it. Every
`retrain` (a month by default) it ranks the rules on how they did out of sample
over the `train_history` before -- with `selection.rank`, a rule's history and
interval in hours being its grid axes -- and trades the winner until the next
retraining. Each rule starts as soon as its own history allows, so a
three-day rule is not held back by a two-month one; a retraining ranks the
rules that traded the whole of its window, and longer ones join as they do.
One candidate is always **no switching**: the parameter set of the
grid's `default_value`s (with the group's own values for grouped parameters),
held throughout; `"baseline": "once"`, or a default set that was never run,
means the set chosen once at the start of the test instead. A retraining where
no candidate passes the filters trades the baseline.

The trained curve never sees the hours it trades, so it is compared with the
baseline directly: the daily pnl5 and pnl10 difference by a moving-block
bootstrap (`block_days`, `resamples`; a one-sided p-value), and the retrainings
won by a sign test. `summary.json`'s `current` is the rule chosen on the latest
window, the one to trade now. Each group gets its own folder under
`post_analysis/train/`, whole and ready to chart. Comparing several training
settings by their p-values is the same in-sample fit one level up: decide them
before looking.

[doc/walk_forward.md](doc/walk_forward.md) collects the idea, the settings,
the training's files and how to read them.

## Commands

| Command | Does |
|---|---|
| `enoshima run` | Run a backtest, or a parameter sweep; `--weekly` for the cumulative weekly run |
| `enoshima select` | Recommend from the recorded weekly runs, per range and group |
| `enoshima walk-forward` | Test the selection rule's window, history and reselection interval; `--train` chooses among them monthly against no switching |
| `enoshima results` | Recorded runs for an environment, best first |

To clear an environment's results, delete its `run_log/`, `best_result/` and `post_analysis/`.

Flags on `run`: `--optimize grid`, `--chunk-days` (memory),
`--max-concurrency` (parameter sets per data pass), `--force` (re-run a parameter
set that already has a result). The variant is `strategy_param_grid.json`'s, the data is
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
