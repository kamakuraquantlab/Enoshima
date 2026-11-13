# Enoshima — Agent Entry Point

Public backtest engine, part of Kamakura Quant Lab. Read [README.md](README.md)
first; this page holds what will cost you if you miss it.

## What this repository is, and is not

It is the CLI backtest engine: replay recorded bronze data through a strategy and
a simulated exchange, and write the results back to the data lake the bronze
came from.

It also serves what it wrote: `enoshima web` (`enoshima.web`, the page in
`web/`, the `web` extra) shows every run in the lake as a page.

It does **not choose**. Recommending a parameter set from a sweep, and testing
how often to choose again (selection, walk-forward), is research on what the
lake holds; it was here once and was moved out. A run writes results; the page
shows them; nothing here ranks them.

It is **not** the seller's backtest operations tooling. The queue that launches
runs from a web page and any production strategy's Grafana dashboard stayed
private, because they spawn subprocesses, name a strategy, and exist to operate
one particular machine. The results viewer came here once it did none of that:
it reads the lake, names no strategy or dashboard (an env's
`grafana_dashboard.json` does), and launches nothing. Keep it that way: **no
endpoint that runs, queues or configures a backtest**, and no strategy, env or
dashboard name in `web.py` or `web/`.

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

A third, `qate-env`, owns the run directory lifecycle and the convention for
resolving a strategy's `Config` and `Variant` classes. Enoshima owns its strategy
definition files and loader.

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

Results go the other way, and are Enoshima's own: `lake` writes them as
Parquet under the root hase reports (`hase.bronze.data_root()`), in the
`research/` layer, with pyarrow, which hase needs anyway. The layout of a
result dataset is Enoshima's to decide, because nothing else writes it; where
the lake *is* stays komachi's answer.

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
2. **Never make `BacktestGateway` optional.** `backtest.Backtest` builds one per
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
  the range *is* reported, and its PnL update's `open_ts` is the earlier time.
- **A backtest reports through `qate`'s `Reporter` interface, like a live run.**
  `backtest.BacktestRuntime` is the synchronous counterpart of qate's `Runtime`.
  Two reporters are always attached: `ResultReporter`, which evaluates and
  keeps the closed trades a result is computed from, and `records.RecordReporter`,
  which keeps the fills, PnL updates, bars and other metrics, column by column.
  Neither writes while the replay runs: `Runner._save` hands a finished
  backtest's trades and records to `lake.Store.write_part`, so a run that fails
  writes nothing.
- **Every result is in the data lake, and none in the env.** `research/`
  holds one dataset per kind -- `BacktestRun`, `BacktestPart`, `BacktestTrade`,
  `BacktestOrder`, `BacktestPnl`, `BacktestBar`, `BacktestMetric` -- each
  partitioned `env=<env>/run_id=<id>/` (and `part=<part>/` but the run's row).
  The env directory holds definitions and `env.log`. Any window or grouping is
  recomputed from `BacktestTrade` without a replay; `BacktestRun` keeps each
  `ledger.GROUPINGS` of a run's trades as `by_<name>`, so a reader comparing
  every run's months need not read every trade. A `BacktestPnl` close update
  carries `PNL`, `FEE`, `notional` and `open_cost`, which is all a trade's
  `pnl_change{0,5,10}` is computed from; keep it that way, since a reader of
  the lake may take a run's curve from it. The names are not `Backtest_Bar` and `Backtest_Pnl`: those
  are earlier tools' datasets in the same layer, partitioned by strategy and
  market, and two layouts must not share a dataset directory. There was once a
  parquet copy of the *whole* metric stream under `result/` (`timeseries.py`);
  do not name a folder `result/` again.
- **A part is written whole or not at all.** A part is a week of a weekly run,
  `all` of a temporary one. `Store.write_part` removes the part's row and the
  run's, writes each data file under a temporary name and renames it, removes
  any file of an earlier write it has no rows for, and writes the part's
  `BacktestPart` row last; the run's `BacktestRun` row follows, and it is what
  "finished" means to the skip check, the viewer and any reader of the lake. A part's row
  records each file's row count, and a reader refuses a part whose files do not
  match it: a lake's housekeeping may delete old `data.parquet` files outside
  bronze, and a part missing its trades must not read as a part that traded
  nothing.
- **Runs in the lake are found by env name.** Two env roots holding an env of
  one name share its results, as their run ids already were the same. A tool
  that runs a copy of an env (`tools/weekly_consistency.py`) passes `Runner`
  a `lake_root` of its own, or it writes over the env's results; the command
  never takes one.
- **Enoshima does not configure logging.** Every command enters its env, and
  `qate_env.Env` attaches the `env.log` handler. Each module takes
  `LOG = getLogger(__name__)` and logs; nothing calls `print`, `basicConfig` or
  adds a handler, and nothing goes to stdout. The one thing it sets is a level:
  `enoshima run` sets the strategy's package and `qate.trading` to
  `enoshima.json`'s `strategy_log_level`, `WARNING` by default, because a live
  run's INFO line per fill became 600 MB of `env.log` in one sweep.
- **A backtest writes no database.** It wrote InfluxDB directly once, as a
  dashboard's source; its records are now in the lake, and a loader from the
  lake into InfluxDB is a separate tool's job. `influxdb-client` is not a
  dependency, and `enoshima.json`'s `"influx"` is accepted and ignored with a
  warning, so the envs written before stay runnable. There is no msgpack log
  either: that is the live system's intermediate format, because a trading
  process must never wait on a disk or a database.
- **A reporter that raises fails the backtest**, unlike in qate's `Runtime`. A
  backtest's reporters are its output.
- **A reporter is a sink, not an event source.** `ResultReporter` hands its
  derived `EVAL` results directly to the run's `RecordReporter`. Do not route
  them back through the runtime as strategy metrics.
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
  event made a sweep 2.4x slower, and the batch's one `DtRange` asked for
  `end_ts` (a `datetime.timestamp()`) per event by every reporter made a 45-way
  weekly sweep 7x slower; a reporter keeps its range as floats of its own. Give each backtest its own copy (and note that
  `frozenset(a_frozenset)` is not a copy). Market data is the exception the
  design accepts: the chunk is shared, and a recorded book (`hase`'s
  `RecordedOrderBook`) caches its market price per size for every reader.
- **Metric objects pack enum *values*, not names.** `ExchangeName`, `Symbol`,
  `Measurement`, `Tag` and `Field` are append-only in `qate`: reordering a member
  silently rewrites the meaning of every metric already recorded.
- **A run log's id is a deterministic hash**, not a UUID, of the env, the
  strategy and its revision, the parameters and the range (or, weekly, the preload and overlap). That is what makes a
  sweep resumable, and what `--force` overrides. It was a UUID once, which meant
  the skip check could never fire and every combination was always recomputed.
- **A weekly run (`--weekly`, `weekly`) is the cumulative backtest.** The range
  is widened to the ISO weeks covering it, Monday to Sunday JST, and each week
  is its own replay: `enoshima.json`'s `weekly.preload_days` before the
  Monday and `weekly.overlap_days` after the Sunday. A trade is the week's
  if it *opened* in the week, wherever it closed: the overlap is there to see
  it closed, and a trade opened in the overlap or the preload is a
  neighbour's. Each week is clipped to the complete days on disk, the last
  less the overlap:
  it runs as far as the data goes, and a partial week is run again by the
  next weekly run whose data reaches further into it -- `_weeks_to_run`
  decides this, not the skip check a temporary run uses. Its run id has no dates and has the
  preload and overlap, so every weekly run of a parameter set adds to one run id: weeks
  are its parts, `part=<YYYY-Www>`, each written whole, and its `BacktestRun`
  row is rebuilt from all of them (`weekly.rebuild`) -- never the current run's
  result -- over the first Monday to the last Sunday. That is what lets
  any reader treat it as one run over its range, and why a run fills the gap
  between the weeks on disk and the weeks asked for, and a run with a gap
  has no row. A week is byte-identical whichever run computed it,
  and `tests/test_weekly.py` holds it: do not let state cross from one
  week's replay into the next, and do not compute a week's or a window's
  figures any way other than `summarize` -- which is also why a strict run
  counts only its own trades' orders, never fills an open order made in the
  overlap (`OverlapGateway`: an add-on would move the position's `open_ts`
  into the next week), and stops replaying once the overlap has closed the
  week's last position. Equality with a continuous replay is not
  promised; `open_at_end` shows where it can fail. A week's records -- its
  orders and PnL updates -- are its own trades' too, so they partition across
  weeks as the trades do, and a week written again replaces every file of it.
- **Every run id includes the strategy's revision** (`strategy_env.strategy_rev`):
  a hash of the variant's own source -- its module and every module of its
  package it imports, followed through the import statements, with each
  package's `__init__.py` -- spelt `src.<12 hex>`. A change to what the variant
  runs must be a new run id, or a weekly run would stitch weeks of two versions
  together, and a reader comparing runs could not tell the two apart. A change that does not
  reach the variant -- a new `vNNN.py` beside it, a test, a note -- must not
  be: it was the checkout's commit once, and every experiment committed moved
  every older variant's results to a new run id. The commit is still recorded,
  as the meta's `strategy_commit` (`+dirty.<hash>` when uncommitted), and is
  provenance only. A module imported by a string is not followed; a strategy
  that does that must name it in an import statement too.
  The meta's `git_rev` is `qate_env`'s own and says nothing about the strategy.
- **Runs from the commit-revision days are migrated, not replayed**
  (`revisions`). `enoshima migrate-revisions` gives a run recorded at a
  clean commit the source revision of its variant at that commit, read with
  `git show`, and moves it to the id that gives; one recorded `+dirty` names
  no source anyone can read back and is left alone. `enoshima merge` joins
  two weekly runs of one parameter set split across revisions, only when both name
  the same source or `--unverified` says so -- recorded in the copied weeks and
  the run's row -- and never when a week both hold differs. The merged-from run
  loses its row and moves to `run_id=<id>.merged`, which `lake.Store` does not
  list: nothing is deleted. Both are dry runs without `--apply`.
- **Results from before the lake are imported, not replayed** (`legacy`).
  `enoshima import-run-log` turns an env's `run_log/` folders into parts and
  rows under their own run ids; a dry run without `--apply`, and it removes
  nothing. The `best_result/` and `post_analysis/` folders an earlier version
  wrote beside them were choices, not results, and are not imported.
- **A backtest's fee rates are `enoshima.json`'s `fee_rates`** (`fees`). qate
  reads a rate off the market's `ExchangeAdapter`, which only a venue package
  registers, and a backtest installs none -- so every fill was once costed at
  zero, with a warning per tracker as the only sign. `fees.register` gives qate
  fee-only adapters; a traded market without a rate refuses the run. Non-zero
  rates are in the run id, zero ones are not, so stating a zero kept every
  earlier run. Do not fill the gap by depending on a venue package.
- **Extra slippage is charged at the close, on both legs** (`Evaluation`),
  the open leg on `open_cost`, as qate defers the open's fee. Charged at the
  open, a position opened in warmup lost that leg's cost.
- **A parameter set's id is Enoshima's, built from `strategy_param_grid.json`** (`param_id`):
  the env name, then the values as plain decimals in grid order. There are no
  scaling factors: they made ids unreadable without the grid, and a missing one
  changed every InfluxDB bucket name, which it once started. Nothing about the
  config is in it: an env's market and strategy are fixed, so its name stands
  for them. It is a column of `BacktestRun`, for finding a parameter set by, so
  the same values must keep giving the same id. It was each strategy's
  `get_param_set_id` once; do not put it back on `Strategy`.
- **A re-run replaces; it never adds.** The lake's files are the run's id, so a
  `--force` re-run, or a week run again, replaces what was there. The batch id
  is recorded in each part's meta, never the run's id, which must stay
  deterministic for a sweep to resume.
- **Nothing here chooses.** `enoshima run` writes results and summaries, and
  there is no command that ranks them: `select` and `walk-forward` were here,
  with `selection`, `best` and `post_analysis`, and were moved out, with
  `enoshima.json`'s `"selection"` and `"walk_forward"` -- now accepted and
  ignored with a warning (`settings.RETIRED`). Do not add a recommendation, a
  score or a ranking back to the CLI, the lake or the viewer. A weekly run is a
  parameter set's lasting record; any other run is temporary
  (`is_temporary_run`), a quick look at some days, with the range and revision
  in its id.
- **A single run uses the defaults in `strategy_param_grid.json`.** An optimize
  sweep expands the values in that same file, so there is one source of parameter
  definitions.
- **A backtest never reloads parameters.** `BacktestRuntime` builds its `Trader`
  with `reload_params=False`; a wall-clock reload would replace a sweep member's
  set partway through a deterministic replay.
- **The command takes no locations.** Data is komachi's: hase asks
  `komachi.data_root()`, which reads `ROOT_PATH` from komachi's settings file and
  nothing else -- no flag, no environment variable, no override passed down from
  here. Komachi is the only source of truth for the root. Output goes back to that
  same lake, the variant is `strategy_param_grid.json`'s, and fees and sizing are
  `enoshima.json`'s (`enoshima.settings`). Do not add `--data-root`, `--output-root`, `--variant` or
  `--influx` back: each was a second answer to a question a file already answers.
  `--chunk-days` and `--max-concurrency` stay as per-run overrides.
- **A run takes its environment's lock and logs there**, so never point one at
  an environment a live process is running from; copy the definition instead.
- **`enoshima.json` refuses unknown keys.** A misspelt `"weekly"` silently
  ignored would run with the defaults with nothing to say so. `"influx"`,
  `"selection"` and `"walk_forward"`, keys that were, are ignored with a
  warning (`settings.RETIRED`).
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
- **Never delete a user's results.** There is no `clean` command: a user who
  wants them gone deletes the env's folders in the lake. An earlier version
  pruned run logs older than seven days automatically, on every run.
- **The viewer reads only what the commands write.** `web.py` serves a run
  as the files its folder once held -- `meta.json`, `summary.json`,
  `trades.csv` and `by_*.json`, read from the run's rows and trades -- its
  records as `orders.csv`, `pnl.csv`, `bars.csv` and `metrics.csv`, named
  from `lake` and `ledger`, so
  a new dataset is served once it is added there and not before. Whatever the
  page shows is computed in the browser from those or read from them; a figure
  the page needs that the lake does not hold belongs in the command that writes
  it, not in a second computation here. A date range on the table is the one
  figure it computes, and with `weekly.summarize` over the run's trades, as
  any window is. It deletes a run's files when asked and
  changes nothing else. Flask is the `web` extra so that the engine
  installs no web server: `cli.cmd_web` imports `enoshima.web` only when run.
- **A strategy is imported by module name, and is the user's to make importable**
  -- installed, or on `PYTHONPATH`. Enoshima does not add the launch directory to
  `sys.path`.

## Where a change goes

| Adding | Goes in |
|---|---|
| A generic indicator, chart, risk rule, or store | `qate`, not here |
| Anything about the run directory, its files, or credentials | `qate-env`, not here |
| A venue, or anything that connects | a venue package, never on a backtest's path |
| A production strategy | its own private repository |
| A way of reading recorded data, or converting it to qate objects | `hase` |
| Where a file lives, or whether a day is complete | `komachi` |
| Replay scheduling, batching, evaluation, the CLI | here |
| The viewer's pages and the API that serves runs from the lake | here, `web.py` and `web/` |
| A result dataset's layout in the lake | here, `lake.py` |
| A strategy's dashboard, or its link from the viewer | the strategy's repository, and the `ENOSHIMA_API` env |
| Choosing a parameter set from results: selection, walk-forward, a ranking | not here: a reader of the lake |

The example strategy is documentation. Keep it the most ordinary thing that works
and keep it honest about losing money; it is there to show the contract, not an
idea.

## Before publishing

```bash
pytest
python3 -m build && cd dist && unzip -q *.whl
grep -rniE 'AKIA|s3://|BEGIN [A-Z ]*PRIVATE KEY|/home/|api[_-]?key *=' .
```

The scan is on the wheel: what ships is the wheel. Add the names of any
private repositories and strategies of your own to the pattern; that list
cannot live in a public file.

The failure this catches is usually not a leaked credential. It is a public
package written as though its reader works here: a reference to a repository they
cannot open, or a dashboard they cannot load, explaining a decision they cannot
check.
