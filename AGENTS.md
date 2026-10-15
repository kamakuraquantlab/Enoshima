# Enoshima — Agent Entry Point

Public backtest engine, part of Kamakura Quant Lab. Read [README.md](README.md)
first; this page holds what will cost you if you miss it.

## What this repository is, and is not

It is the CLI backtest engine: replay recorded bronze data through a strategy and
a simulated exchange, and write the results to disk.

It is **not** the seller's backtest operations tooling. The REST API, the React
results browser, the Grafana dashboard and the queue that launches runs from a web
page stayed private, because they name a production strategy's dashboard, spawn
subprocesses, and exist to operate one particular machine. If a public reason to
publish a results UI appears, it should be written fresh rather than ported.

## Where the pieces live

The data path is a chain, and each link talks only to the next:

    enoshima ──► hase.bronze ──► komachi.bronze
     replay       Market, Trade,   which file holds a market-day,
                  OrderBook        and whether it is complete

- `komachi` downloads files and places them in the data lake layout. It knows the
  root, the paths and the checksums, and never what is inside a file. It depends
  on nothing else in the ecosystem.
- `hase` is the data lake operator, and speaks trading: it depends on `qate`,
  lists markets as `Market`s, and turns a request for "these order books, these
  days" into `komachi.bronze.file(...)` calls, opens them, and returns `Trade`s
  and `OrderBook`s. It also builds silver and gold from bronze.
- `qate` owns the model and the replay machinery, and depends on neither.

A third, `qate-env`, owns the run directory: `Env`, the credential lookup, and the
`trading.json` / `config.json` / `params.json` loader. It was `qate.env` and
`qate.boot` until the split, so an import error naming either of those is a stale
checkout rather than a missing feature. A live runner reads the same package, which
is the reason a backtest can be pointed at a production environment at all --
keep reading it, and keep writing results somewhere else.

**Enoshima calls `hase.bronze` and nothing below it.** `data_source.Stream` asks
it for a market-day, tags each object with its `EventType`, and that is all the
data code here. No `import komachi`, no path, no parquet, no frame conversion:
komachi is not even a direct dependency. Which markets are on disk, which days
are complete, how a book's depth is read -- each is hase's answer, and a change
to any of it goes in hase.
This was `qate.data.bronze` once, which globbed the tree itself, and then
`enoshima/bronze.py`, which still converted frames and asked komachi for the
root. Each was a second copy of an answer that belongs elsewhere.
`komachi.bronze`'s docstring is the argument.

A consequence worth protecting: `qate` now installs no networking library at all,
because dropping the data reader dropped its komachi dependency and with it the
transitive `httpx`. Do not put a data dependency back into `qate` to make
something here easier.

## The invariant it inherits

**A backtest must never be able to reach an exchange.** Enoshima does not enforce
this itself — `qate` does, by having no networking library in its dependency tree —
but it is easy to break from here:

1. **Never add a venue client, an exchange SDK, or a WebSocket library to
   `dependencies`.** A backtest reads files.
2. **Never make `SimulatorGateway` optional.** `backtest.Backtest` builds one per
   exchange in the config; there is no code path that substitutes a real gateway,
   and there should not be.
3. **No credential, no private path, no production environment name** in source,
   tests, docs or the example.

## Facts worth knowing before editing

- **Time comes from the event.** A strategy uses `self.now_ts`, set from each
  event's timestamp. `time.time()` still runs and still produces numbers, and
  those numbers compare a wall clock against replayed prices. This is the failure
  mode that does not announce itself.
- **An order fills on the book after the one it was created on.** The simulator
  matches against the next snapshot. Do not "fix" it into same-book fills.
- **Warmup consumes events.** `Trader.before_loop` drains into `Strategy.warmup_*`
  until `is_ready`, and `result.in_range` then drops everything before
  the range start. A test that counts events or metrics must account for both.
  The one intended exception: a position opened during warmup and closed inside
  the range *is* reported, and its open-aligned PnL copy carries the earlier
  timestamp.
- **A backtest reports through `qate`'s `Reporter` interface, like a live run.**
  `backtest.BacktestRuntime` is the synchronous counterpart of qate's `Runtime`.
  `ResultReporter` is always attached and is what a result is computed from;
  `InfluxReporter` is attached only when the env's `enoshima.json` has an
  `"influx"` entry. A run without InfluxDB must still finish with its result.
  `influxdb-client` is a plain dependency, not an extra: the import is not the
  optional part, the recording is. The token and URLs are `enoshima.json`'s,
  not a credential lookup -- the InfluxDB is ours, unlike the external services
  the credential files are for.
- **Trading data goes to InfluxDB and nowhere else.** Orders, PnL and bars are
  `InfluxReporter`'s; `ResultReporter` only evaluates. There was a parquet copy
  under `result/` (`timeseries.py`); it duplicated the metric stream and was
  removed. Do not bring it back.
- **Enoshima does not configure logging.** Every command enters its env, and
  `qate_env.Env` attaches the `env.log` handler. Each module takes
  `LOG = getLogger(__name__)` and logs; nothing calls `print`, `basicConfig` or
  adds a handler, and nothing goes to stdout. The one thing it sets is a level:
  `enoshima run` sets the strategy's package and `qate.trading` to
  `enoshima.json`'s `strategy_log_level`, `WARNING` by default, because a live
  run's INFO line per fill became 600 MB of `env.log` in one sweep.
- **There is no msgpack log.** That is the live system's intermediate format --
  written in real time, uploaded, then loaded into InfluxDB -- because a trading
  process must never wait on a database. A backtest can, so it writes InfluxDB
  directly. Do not reintroduce a local metric file "to be safe".
- **A reporter that raises fails the backtest**, unlike in qate's `Runtime`. A
  backtest's reporters are its output.
- **The shared buffer is the only concurrency, and it can deadlock.** Every
  `Channel` blocks in `request_load_more` until all of them have arrived, so a
  backtest thread that dies without draining its channel strands the rest.
  `Runner._run_batch` collects each future's exception for exactly this reason;
  do not replace it with a bare `wait()`.
- **Every chunk is loaded on one dedicated thread**, not by whichever backtest
  reaches the barrier last. On free-threaded Python (`run.sh`) each thread
  allocates from its own heap, and a chunk allocated on one thread and freed on
  another was not reused: a 45-way sweep grew by about 1 GB per replayed day
  until it ran out of memory, and a 9-way one grew too. Loading and releasing on
  the same thread kept it flat, with identical results. That thread also loads
  the next chunk while the current one is replayed, so two chunks can be in
  memory at once; `SharedBuffer.reset` stops it before the loader is rewound.
- **No Python object may be touched by every backtest thread on every event.**
  On free-threaded Python, threads reading one shared object contend on its
  reference count; a single `frozenset` shared by 45 strategies and checked per
  event made a sweep 2.4x slower. Give each backtest its own copy (and note that
  `frozenset(a_frozenset)` is not a copy). Market data is the exception the
  design accepts: the chunk is shared, and a recorded book (`hase`'s
  `RecordedOrderBook`) caches its market price per size for every reader.
- **Metric objects pack enum *values*, not names.** `ExchangeName`, `Symbol`,
  `Measurement`, `Tag` and `Field` are append-only in `qate`: reordering a member
  silently rewrites the meaning of every metric already recorded.
- **A run log's id is a deterministic hash**, not a UUID. That is what makes a
  sweep resumable, and what `--force` overrides. It was a UUID once, which meant
  the skip check could never fire and every combination was always recomputed.
- **A parameter set's id is Enoshima's, built from `param_grid.json`** (`param_id`):
  the env name, then the values as plain decimals in grid order, plus
  `_tmp_<timestamp>` for `--temporary`. There are no scaling factors: they made
  ids unreadable without the grid, and a missing one changed every bucket name. Nothing about the config is in it: an env's
  market and strategy are fixed, so its name stands for them. It names the InfluxDB
  bucket, so the same values must keep giving the same id. It was each strategy's
  `get_param_set_id` once; do not put it back on `Strategy`.
- **`post_analysis` ranks across every run log with the same strategy and range**,
  not just the current batch. A resumed sweep must be able to pick a best from
  results it did not just compute.
- **A single run's parameters come from `params.json`**, the same file a live run
  reads, so "backtest what production runs" is one command. `param_grid.json` is a
  search space and only `--optimize` consults it. These were one source once -- the
  grid's defaults -- which meant an environment had two answers and nothing kept
  them in step; the backtest envs left `params.json` empty and the live envs left
  the grid alone. An empty `params.json` still falls back to the grid's defaults.
- **A backtest never reloads `params.json`.** A live `Trader` re-reads it every
  ten wall-clock seconds and hands it to the strategy; `BacktestRuntime` builds
  its `Trader` with `reload_params=False`. With the reload on, every combination
  of a sweep is replaced by the file's set partway through, and the sweep ranks
  one strategy against itself. It went unnoticed only because `update_params`
  refuses a dict whose keys differ, and the env's file and grid happened to.
- **The command takes no locations.** Data is komachi's: hase asks
  `komachi.data_root()`, which reads `ROOT_PATH` from komachi's settings file and
  nothing else -- no flag, no environment variable, no override passed down from
  here. Komachi is the only source of truth for the root. Output is the env dir, the variant is
  `trading.json`'s, and recording and sizing are `enoshima.json`'s
  (`enoshima.settings`). Do not add `--data-root`, `--output-root`, `--variant` or
  `--influx` back: each was a second answer to a question a file already answers.
  `--chunk-days` and `--max-concurrency` stay as per-run overrides.
- **A run writes into its environment and takes its lock**, so never point one at
  an environment a live process is running from; copy the definition instead.
- **`enoshima.json` refuses unknown keys.** A misspelt `"influx"` silently
  ignored would leave a run unrecorded with nothing to say so.
- **The strategy's `Config` class is resolved from the strategy module**, by
  `qate_env.trading_env.resolve_config_class`. There is no `desc.json` and no index of
  any kind; the map that used to be one went stale whenever code moved. Do not
  reintroduce one.
- **A run log is given what it records, never asked for it.** `RunLog` used to read
  `env.get("config")`, which returned something only if the env had been loaded
  through the old map -- so when Enoshima stopped doing that, run logs silently
  recorded `"config": null` for two commits. `tests/test_run_end_to_end.py` now
  holds the provenance fields, because a result you cannot attribute to a
  configuration is not a result.
- **`--optimize` scores on `pnl5`**, the middle slippage assumption -- ranking on
  `pnl0` selects for strategies whose edge is thinnest -- but does not simply
  take the highest. `selection` filters (no loss at pnl10 by default), smooths
  each score over its grid neighbours, and walk-forward tests the choice on JST
  months from `result.periods`, recommending what it chose on the latest months.
  The out-of-sample total is the honest figure; an in-sample best is not one.
  `selection` works only from run logs: it must never need a replay.
- **Never delete a user's results.** There is no `clean` command: a user who
  wants them gone deletes `run_log/` and `best_result/`. An earlier version pruned
  run logs older than seven days automatically, on every run.
- **A strategy is imported by module name, and is the user's to make importable**
  -- installed, or on `PYTHONPATH`. Enoshima does not add the launch directory to
  `sys.path`.

## Where a change goes

| Adding | Goes in |
|---|---|
| A generic indicator, chart, risk rule, or store | `qate`, not here |
| Anything about the run directory, its files, or credentials | `qate-env`, not here |
| A venue, or anything that connects | `qate-exchanges`, and not on a backtest's path |
| A production strategy | its own private repository |
| A way of reading recorded data, or converting it to qate objects | `hase` |
| Where a file lives, or whether a day is complete | `komachi` |
| Replay scheduling, batching, evaluation, the CLI | here |

The example strategy is documentation. Keep it the most ordinary thing that works
and keep it honest about losing money; it is there to show the contract, not an
idea.

## Before publishing

```bash
pytest
python3 -m build && cd dist && unzip -q *.whl
grep -rniE 'AKIA|s3://|BEGIN [A-Z ]*PRIVATE KEY|/home/|api[_-]?key *=' .
```

The scan is on the wheel: what ships is the wheel. `qate-exchanges`' AGENTS.md
carries the list of private ecosystem names to add to the pattern — that list
cannot live in a public file.

The failure this catches is usually not a leaked credential. It is a public
package written as though its reader works here: a reference to a repository they
cannot open, or a dashboard they cannot load, explaining a decision they cannot
check.
