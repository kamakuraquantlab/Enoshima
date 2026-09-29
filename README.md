# Enoshima

A backtest engine for recorded crypto market data. It replays the order books and
trades you already have on disk through a strategy and a simulated exchange, and
writes what happened next to them.

It cannot reach an exchange. That is a property of what it is built on:
[`qate`](https://github.com/kamakuraquantlab/qate) supplies the event loop, the
strategy base class and the simulator, and has no WebSocket client anywhere in
its dependency tree. A backtest here is offline and deterministic because there
is no other kind of run available.

```bash
pip install kamakuraquantlab-enoshima
```

Named for the island off the Shonan coast, the far end of the line. Komachi is
where you get the data, Hase is where you look at it, Enoshima is where you find
out whether an idea would have worked.

## Where the data comes from

[Komachi](https://github.com/kamakuraquantlab/Komachi) downloads it, and Enoshima
reads it where Komachi put it — no path to configure:

```bash
komachi get --market COINCHECK:BTC_SPOT --start 2026-09-01 --end 2026-09-07
enoshima markets
```

```text
Data root: ~/kamakuraquantlab-data
market                       dataset      days  range
COINCHECK:BTC_SPOT           Trade           7  2026-09-01 .. 2026-09-07
COINCHECK:BTC_SPOT           OrderBook       7  2026-09-01 .. 2026-09-07
```

`--data-root` reads any tree in the same Hive-partitioned layout. Dates are
Asia/Tokyo days spanning 15:00–14:59 UTC; the timestamps inside the files are UTC
epochs.

## Quick start

The repository ships a runnable example: a moving-average crossover and an
environment that backtests it.

```bash
git clone https://github.com/kamakuraquantlab/Enoshima && cd Enoshima
pip install -e .

export QATE_ENV_ROOT=$PWD/example/env
enoshima run --env EXAMPLE_BACKTEST --from 2026-09-01 --to 2026-09-01
```

```text
[13:48:54] START env=EXAMPLE_BACKTEST strategy=example.strategy.v1 range=2026-09-01 00:00:00..2026-09-01 23:59:59 mode=single combos=1
[13:49:14] Loaded 2026-08-31..2026-09-01: 269463 events
[13:49:16] RESULT params=(fast_period=20 slow_period=120) pnl5=-7474.41 trades=1292 sharpe=-261.631
```

It loses money, and it is supposed to: a crossover that crosses 1,292 times in a
day pays the spread 1,292 times. That is the first thing a backtest is for.

Then sweep the grid in `param_grid.json`:

```bash
enoshima run --env EXAMPLE_BACKTEST --from 2026-09-01 --to 2026-09-01 --optimize grid
enoshima results --env EXAMPLE_BACKTEST
```

```text
        pnl5  trades   sharpe  range                                     params
    -4005.76     671 -139.398  2026-09-01 00:00:00..2026-09-01 23:59:59  fast_period=40 slow_period=240
    -9621.36    1632 -339.923  2026-09-01 00:00:00..2026-09-01 23:59:59  fast_period=10 slow_period=120
```

## Writing a strategy

Copy `example/strategy/`. Two files, and the contract is small:

- **`config.py`** — a `BootConfig` saying which markets and event types to replay.
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
{ "strategy_module_name": "my_strategy", "variant": "v1", "gateway_name": "SIMULATOR" }
```

## An environment

One directory per named run configuration, under `~/env` (or `QATE_ENV_ROOT`):

| File | Holds |
|---|---|
| `desc.json` | Which class loads each of the files below |
| `config.json` | The strategy's `Config` — market, order size |
| `trading.json` | Which strategy module and variant to run |
| `param_grid.json` | The default parameters, and the values a sweep walks |
| `params_combo.json` | An explicit parameter list, for `--optimize combo` |
| `features.json` | Feature flags |

A run writes back into the same directory:

```text
EXAMPLE_BACKTEST/
├── run_log/<id>.json                  one per parameter set: provenance + results
├── best_result/<batch_id>.json         the winner of a sweep
└── result/<batch_id>/<param_set_id>/
    ├── Metrics_20260901.msgpack        orders, PnL, evaluations
    └── dataset=Pnl/.../data.parquet    per-trade PnL
```

## Results are local, and a database is optional

Everything above is written to disk as the run produces it. Nothing contacts a
server, so a run finishes whether or not one is reachable, and a failed export
can simply be repeated.

To chart results in Grafana:

```bash
pip install 'kamakuraquantlab-enoshima[influx]'
enoshima export --env EXAMPLE_BACKTEST
```

One bucket per parameter set, `backtest_<ENV>_<param_set_id>`, so a sweep's
combinations sit side by side. Connection details come from
`~/.qate/influxdb.keys`.

## Every run is evaluated three times

At 0, 0.005% and 0.01% extra slippage, reported with the suffixes `0`, `5` and
`10` — `pnl0`, `pnl5`, `pnl10`. One number is not a result. A strategy whose edge
survives at zero slippage and vanishes at 0.005% has told you something important
about itself, and reporting only the optimistic figure hides it. `--optimize`
ranks on `pnl5` for the same reason.

## Commands

| Command | Does |
|---|---|
| `enoshima run` | Run a backtest, or a parameter sweep |
| `enoshima markets` | What market data is on disk |
| `enoshima results` | Recorded runs for an environment, best first |
| `enoshima export` | Load a run's metrics into InfluxDB |
| `enoshima clean` | Delete an environment's results (needs `--yes`) |

Useful flags on `run`: `--optimize grid|combo`, `--variant`, `--data-root`,
`--chunk-days` (memory), `--max-concurrency` (parameter sets per data pass),
`--force` (re-run a parameter set that already has a result).

## Memory, and how a sweep is cheap

A day of full-depth order books is hundreds of megabytes once it is Python
objects, so the range is loaded a couple of days at a time into **one** buffer
that every parameter set in a batch reads from. A sweep therefore costs
`ceil(combos / --max-concurrency)` passes over the data, not one pass per
combination — five parameter sets over a day of real books take about as long as
one.

If a run is killed by the OOM reaper, lower `--chunk-days` before anything else.

Runs are also resumable. A run log's id is a hash of the environment, strategy,
parameters and date range, so a sweep that dies part way through resumes where it
stopped, and the combinations it skips are still ranked when it picks a best.

## Development

```bash
pip install -e '.[dev]'
pytest
```

`tests/test_run_end_to_end.py` drives a real run from synthetic parquet to
results on disk, which is the test to read first and to keep passing.

## Licence

Apache 2.0. See [LICENSE.md](LICENSE.md).
