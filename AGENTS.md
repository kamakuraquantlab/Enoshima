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

Enoshima is the join between two packages that know nothing about each other.
`komachi` owns the data and answers where it is; `qate` owns the model and the
replay machinery. Neither depends on the other, and neither should.

A third, `qate-env`, owns the run directory: `Env`, the credential lookup, and the
`trading.json` / `config.json` / `params.json` loader. It was `qate.env` and
`qate.boot` until the split, so an import error naming either of those is a stale
checkout rather than a missing feature. A live runner reads the same package, which
is the reason a backtest can be pointed at a production environment at all --
keep reading it, and keep writing results somewhere else.

`bronze.py` is that join and the only place that speaks both vocabularies —
komachi's `"EX:SYM"` strings and qate's `Market`.

**Never reimplement what komachi already answers.** Paths, which days are
complete, which markets are on disk: ask `komachi.bronze` and `komachi.layout`.
This module was originally `qate.data.bronze`, which globbed the tree itself and
duplicated those answers with a docstring promising by hand that the two agreed.
That promise is not maintained by anything. `komachi.bronze`'s own docstring is
the argument, and it is worth reading before changing this file.

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
  `"influx"` entry. A run without InfluxDB must still finish with its result, so
  nothing outside `influx.py` and that path may import `influxdb_client`.
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
- **Metric objects pack enum *values*, not names.** `ExchangeName`, `Symbol`,
  `Measurement`, `Tag` and `Field` are append-only in `qate`: reordering a member
  silently rewrites the meaning of every metric already recorded.
- **A run log's id is a deterministic hash**, not a UUID. That is what makes a
  sweep resumable, and what `--force` overrides. It was a UUID once, which meant
  the skip check could never fire and every combination was always recomputed.
- **`post_analysis` ranks across every run log with the same strategy and range**,
  not just the current batch. A resumed sweep must be able to pick a best from
  results it did not just compute.
- **A single run's parameters come from `params.json`**, the same file a live run
  reads, so "backtest what production runs" is one command. `param_grid.json` is a
  search space and only `--optimize` consults it. These were one source once -- the
  grid's defaults -- which meant an environment had two answers and nothing kept
  them in step; the backtest envs left `params.json` empty and the live envs left
  the grid alone. An empty `params.json` still falls back, with a warning.
- **The command takes no locations.** Data is komachi's (`komachi.data_root`,
  which honours `ROOT_PATH` for one run), output is the env dir, the variant is
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
- **`--optimize` ranks on `pnl5`**, the middle slippage assumption. Ranking on
  `pnl0` selects for strategies whose edge is thinnest.
- **Never delete a user's results on a schedule.** `enoshima clean` requires
  `--yes` and is the only thing that removes them. An earlier version pruned run
  logs older than seven days automatically, on every run.

## Where a change goes

| Adding | Goes in |
|---|---|
| A generic indicator, chart, risk rule, or store | `qate`, not here |
| Anything about the run directory, its files, or credentials | `qate-env`, not here |
| A venue, or anything that connects | `qate-exchanges`, and not on a backtest's path |
| A production strategy | its own private repository |
| A way of reading recorded data | `bronze.py` here, delegating to komachi |
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
