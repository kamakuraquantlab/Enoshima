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

## Documentation

| Page | Covers |
|---|---|
| [doc/00_overview.md](doc/00_overview.md) | The whole picture: reading from hase, the environment and its configs, weekly and temporary runs, where results go |
| [doc/01_weekly_run.md](doc/01_weekly_run.md) | The cumulative weekly run: preload, overlap, which week a trade belongs to, how weeks are saved |
| [doc/04_web.md](doc/04_web.md) | `enoshima web`: the results viewer, its env, dashboard links and API |

## Where the data comes from

[Komachi](https://github.com/kamakuraquantlab/Komachi) downloads it, and Enoshima
reads it through [Hase](https://github.com/kamakuraquantlab/Hase) where Komachi
put it. There is no path to configure:

```bash
komachi download --market COINCHECK:BTC_SPOT --start 2026-09-01 --end 2026-09-07
komachi local
```

How the layers divide the work is in [the overview](doc/00_overview.md#where-the-data-comes-from-hase).

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

A run without `--weekly` like this one is a quick look: kept and listed by
`results`, with its range in its id. The main use is weekly
([doc/01_weekly_run.md](doc/01_weekly_run.md)): sweep the grid in
`strategy_param_grid.json` over whole weeks, and every later run adds the
weeks it has data for:

```bash
enoshima run --env EXAMPLE_BACKTEST --from 2026-09-01 --to 2026-09-30 --optimize grid --weekly
enoshima results --env EXAMPLE_BACKTEST
```

Enoshima runs backtests and shows their results; it does not choose among
them. Which parameter set to trade is a question for whatever reads the lake.

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

Two rules break a backtest silently rather than loudly (more in
[the overview](doc/00_overview.md#what-it-is)):

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

The environment's other files, `strategy_config.json` and `enoshima.json`, are
described in [the overview](doc/00_overview.md#the-environment).

## Commands

| Command | Does |
|---|---|
| `enoshima run` | Run a backtest, or a parameter sweep; `--weekly` for the cumulative weekly run |
| `enoshima results` | Recorded runs for an environment, best first |
| `enoshima web` | Every env's runs as a page, replaying nothing (the `web` extra) |
| `enoshima import-run-log` | Bring an env's results from before the lake into it |

Results go back to the data lake the data came from, under `research/`, one
dataset per kind -- runs, trades, orders, PnL updates, bars, metrics -- by env
and run id ([the overview](doc/00_overview.md#where-the-results-are)). Results
written into an env before then are brought over with `enoshima import-run-log`.

Flags on `run`: `--optimize grid`, `--chunk-days` (memory),
`--max-concurrency` (parameter sets per data pass), `--force` (re-run a parameter
set that already has a result). The variant is `strategy_param_grid.json`'s, the data is
komachi's, and the output goes back to komachi's data lake.

## Memory, and how a sweep is cheap

A day of full-depth order books is hundreds of megabytes once it is Python
objects, so the range is loaded a couple of days at a time into **one** buffer
that every parameter set in a batch reads from. A sweep therefore costs
`ceil(combos / --max-concurrency)` passes over the data, not one pass per
combination — five parameter sets over a day of real books take about as long as
one. The next chunk is loaded while the current one is replayed, so up to two
chunks are in memory at once.

If a run is killed by the OOM reaper, lower `chunk_days` before anything else.

Runs are also resumable. A run log's id is a hash of the environment, the strategy
and its revision, the parameters and the range (weekly: the preload and overlap), so a sweep that dies part way through resumes where it
stopped, and the combinations it skips keep their results.

## Running on free-threaded Python

A batch's parameter sets are threads. On an ordinary build they take turns on
one core; on a free-threaded build (3.14t) they run at once, and a sweep is
bounded by cores rather than by the GIL. `run.sh` runs `enoshima` that way and
refuses to start on anything else:

```bash
pyenv virtualenv 3.14.0t enoshima-314t
pip install --only-binary=:all: 'pandas>=3.0' numpy pyarrow dacite httpx matplotlib
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
