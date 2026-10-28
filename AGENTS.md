# Enoshima — Agent Entry Point

Public backtest engine, part of Kamakura Quant Lab. Read [README.md](README.md)
first; this page holds what will cost you if you miss it.

## What this repository is, and is not

It is the CLI backtest engine: replay recorded bronze data through a strategy and
a simulated exchange, and write the results to disk.

It also serves what it wrote: `enoshima web` (`enoshima.web`, the page in
`web/`, the `web` extra) shows every run folder and training as a page.

It is **not** the seller's backtest operations tooling. The queue that launches
runs from a web page and any production strategy's Grafana dashboard stayed
private, because they spawn subprocesses, name a strategy, and exist to operate
one particular machine. The results viewer came here once it did none of that:
it reads run folders, names no strategy or dashboard (an env's
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
  the range *is* reported, and its open-aligned PnL copy carries the earlier
  timestamp.
- **A backtest reports through `qate`'s `Reporter` interface, like a live run.**
  `backtest.BacktestRuntime` is the synchronous counterpart of qate's `Runtime`.
  `ResultReporter` is always attached and is what a result is computed from;
  `DatalakeReporter` is always attached and copies BAR metrics into the
  historical research-layer `Backtest_Bar` Parquet layout;
  `InfluxReporter` is attached only when the env's `enoshima.json` has an
  `"influx"` entry. A run without InfluxDB must still finish with its result.
  `influxdb-client` is a plain dependency, not an extra: the import is not the
  optional part, the recording is. The token and URLs are `enoshima.json`'s,
  not a credential lookup -- the InfluxDB is ours, unlike the external services
  the credential files are for.
- **PnL goes to the run folder; bars also go to the data lake; the complete
  chart stream goes to InfluxDB.** A run's
  closed trades are what its result is evaluated from, so `ResultReporter` keeps
  them and `run_dir` writes them as `trades.csv`, with `summary.json` and one
  `by_<grouping>.json` per `ledger.GROUPINGS` beside the provenance in
  `meta.json`. Any window or grouping is then recomputed without a replay.
  `DatalakeReporter` writes only BAR metrics to the research-layer
  `Backtest_Bar` Hive layout, independently of InfluxDB, so offline analysis
  can consume the bars and their strategy-defined indicator fields. InfluxDB
  is for looking at a run: orders, PnL and bars. There was a parquet copy of the
  *whole* metric stream under `result/` (`timeseries.py`), removed because it
  duplicated InfluxDB; do not restore that stream or name a folder `result/`
  again.
- **A run folder is written whole or not at all.** `run_dir.write` builds
  `run_log/<id>.tmp-<pid>/` and renames it into place, and `summary.json` is
  what "finished" means to the skip check. A folder that could exist without
  its trades would be skipped by a resumed sweep forever.
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
- **A reporter is a sink, not an event source.** `ResultReporter` writes its
  derived `EVAL` results directly to the run's optional Influx writer. Do not
  route them back through the runtime as strategy metrics.
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
  preload and overlap, so every weekly run of a parameter set adds to one folder: weeks
  go to `weeks/<YYYY-Www>/`, each written whole, and the root files are
  rebuilt from all of them (`weekly.rebuild`) -- never the current run's
  result -- over the first Monday to the last Sunday. That is what lets
  `select` and `walk-forward` read it unchanged, and why a run fills the gap
  between the weeks on disk and the weeks asked for, and a root with a gap
  has no `summary.json`. A week is byte-identical whichever run computed it,
  and `tests/test_weekly.py` holds it: do not let state cross from one
  week's replay into the next, and do not compute a week's or a window's
  figures any way other than `summarize` -- which is also why a strict run
  counts only its own trades' orders, never fills an open order made in the
  overlap (`OverlapGateway`: an add-on would move the position's `open_ts`
  into the next week), and stops replaying once the overlap has closed the
  week's last position. Equality with a continuous replay is not
  promised; `open_at_end` shows where it can fail. Its InfluxDB bucket is one
  per run, and a week's points are deleted by their `week` tag before it is
  rewritten -- by tag, not time, because a Sunday open has its close-aligned
  copy on the Monday.
- **Every run id includes the strategy's revision** (`strategy_env.strategy_rev`):
  a hash of the variant's own source -- its module and every module of its
  package it imports, followed through the import statements, with each
  package's `__init__.py` -- spelt `src.<12 hex>`. A change to what the variant
  runs must be a new folder, or a weekly run would stitch weeks of two versions
  together; `best.group` ranks one revision at a time. A change that does not
  reach the variant -- a new `vNNN.py` beside it, a test, a note -- must not
  be: it was the checkout's commit once, and every experiment committed moved
  every older variant's results to a new folder. The commit is still recorded,
  as `meta.json`'s `strategy_commit` (`+dirty.<hash>` when uncommitted), and is
  provenance only. A module imported by a string is not followed; a strategy
  that does that must name it in an import statement too.
  `meta.json`'s `git_rev` is `qate_env`'s own and says nothing about the strategy.
- **Folders from the commit-revision days are migrated, not replayed**
  (`revisions`). `enoshima migrate-revisions` gives a folder recorded at a
  clean commit the source revision of its variant at that commit, read with
  `git show`, and renames it to the id that gives; one recorded `+dirty` names
  no source anyone can read back and is left alone. `enoshima merge` joins
  two weekly folders of one run split across revisions, only when both name
  the same source or `--unverified` says so -- recorded in the copied weeks and
  the root -- and never when a week both hold differs. The merged-from folder
  is renamed `<id>.merged`, which `run_dir.iter_complete` skips: nothing is
  deleted. Both are dry runs without `--apply`.
- **A backtest's fee rates are `enoshima.json`'s `fee_rates`** (`fees`). qate
  reads a rate off the market's `ExchangeAdapter`, which only a venue package
  registers, and a backtest installs none -- so every fill was once costed at
  zero, with a warning per tracker as the only sign. `fees.register` gives qate
  fee-only adapters; a traded market without a rate refuses the run. Non-zero
  rates are in the run id, zero ones are not, so stating a zero kept every
  earlier folder. Do not fill the gap by depending on a venue package.
- **Extra slippage is charged at the close, on both legs** (`Evaluation`),
  the open leg on `open_cost`, as qate defers the open's fee. Charged at the
  open, a position opened in warmup lost that leg's cost.
- **A parameter set's id is Enoshima's, built from `strategy_param_grid.json`** (`param_id`):
  the env name, then the values as plain decimals in grid order. There are no
  scaling factors: they made ids unreadable without the grid, and a missing one
  changed every bucket name. Nothing about the config is in it: an env's
  market and strategy are fixed, so its name stands for them. It starts the InfluxDB
  bucket, so the same values must keep giving the same id. It was each strategy's
  `get_param_set_id` once; do not put it back on `Strategy`.
- **A bucket is `backtest_<param_set_id>_<batch_id>`: every execution writes its
  own.** A `--force` re-run used to write into the bucket of the run it repeated,
  leaving the old run's points beside the new. There is no `--temporary` any more;
  it was the opt-in for what is now always true. The batch id goes in the bucket,
  never in the run's id, which must stay deterministic for a sweep to resume.
- **A run does not choose.** `enoshima run` writes results and summaries;
  `select` and `walk-forward` run only when asked. Only weekly runs are
  candidates: any other run is temporary (`is_temporary_run`), a quick look at
  some days, with the range and revision in its id.
- **A recommendation is per range and per group, from every run folder**, not
  per batch (`best.recommend`). Runs are grouped by strategy and range, then by
  `group_by` -- the categorical parameters by default -- and each group is
  ranked alone. A resumed sweep must be able to pick a best from results it did
  not just compute, and a categorical value is a different way of trading, to
  be compared on its own merits rather than outranked on a box size's figures.
  `enoshima select` reruns it from disk. `enoshima walk-forward`
  (`post_analysis.walk_forward`) tests the rule's own window, history and
  reselection interval to the hour, every rule on the same test hours; it reuses
  `selection.rank` and must not grow a second way of choosing. Its rules are
  `enoshima.json`'s `"walk_forward"`, not flags; `history` maps each history to
  its own intervals, because an interval is only sensible against a history.
  `--train` ranks the rules' out-of-sample curves, and a no-switching baseline,
  with that same `selection.rank`, and is compared with the baseline only out
  of sample. There each rule starts when its own history allows, and a
  retraining ranks only the rules that traded its whole window; do not align
  them on the longest history, which held a 3d rule back by two months. Its
  folders under `post_analysis/train/` are written whole, `summary.json` last,
  and a results viewer may read them -- add to their files rather than rename.
- **A single run uses the defaults in `strategy_param_grid.json`.** An optimize
  sweep expands the values in that same file, so there is one source of parameter
  definitions.
- **A backtest never reloads parameters.** `BacktestRuntime` builds its `Trader`
  with `reload_params=False`; a wall-clock reload would replace a sweep member's
  set partway through a deterministic replay.
- **The command takes no locations.** Data is komachi's: hase asks
  `komachi.data_root()`, which reads `ROOT_PATH` from komachi's settings file and
  nothing else -- no flag, no environment variable, no override passed down from
  here. Komachi is the only source of truth for the root. Output is the env dir, the variant is
  `strategy_param_grid.json`'s, and recording and sizing are `enoshima.json`'s
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
- **`select` does not take the highest `pnl5`.** Ranking on `pnl0` selects
  for strategies whose edge is thinnest, and ranking on any one figure selects
  for the set that is exceptional on it. `selection` filters (no loss at pnl10
  by default), scores each set by its mean percentile on pnl5, pnl10, dd5 and
  `top_share5` (how much of its winnings its best 5% of trades made), smooths
  each figure over its grid neighbours, and walk-forward tests the choice on JST
  months from each run's `by_month.json` and `trades.csv`. The window expands:
  each choice uses every month before it, and the set recommended is chosen on
  the whole range. On three trend-following sweeps a rolling six months chose on noise,
  and the pnl5-only score kept choosing the deepest drawdowns; both remain as
  settings (`"window": "rolling"`, `"score": "pnl5"`), not as defaults. The out-of-sample total is the honest figure; an in-sample
  best is not one. `selection` works only from run folders: it must never need
  a replay. `best_result/<batch_id>/` holds `selection.json` and, for each
  group where a set passed the filters, a copy of its `meta.json` as `<run_id>.json`.
- **Never delete a user's results.** There is no `clean` command: a user who
  wants them gone deletes `run_log/`, `best_result/` and `post_analysis/`. An
  earlier version pruned run logs older than seven days automatically, on every
  run.
- **The viewer reads only what the commands write.** `web.py` serves a run
  folder's `meta.json`, `summary.json`, `trades.csv` and `by_*.json` and a
  training's `TRAIN_FILES`, named from `run_dir`, `ledger` and `walk_forward`,
  so a new file is served once it is added there and not before. Whatever the
  page shows is computed in the browser from those files or read from them;
  a figure the page needs that no file has belongs in the command that writes
  the folder, not in a second computation here. It deletes a run folder when
  asked and changes nothing else. Flask is the `web` extra so that the engine
  installs no web server: `cli.cmd_web` imports `enoshima.web` only when run.
- **A strategy is imported by module name, and is the user's to make importable**
  -- installed, or on `PYTHONPATH`. Enoshima does not add the launch directory to
  `sys.path`.

## Where a change goes

| Adding | Goes in |
|---|---|
| A generic indicator, chart, risk rule, or store | `qate`, not here |
| Anything about the run directory, its files, or credentials | `qate-env`, not here |
| A venue, or anything that connects | `k2-exchanges`, and not on a backtest's path |
| A production strategy | its own private repository |
| A way of reading recorded data, or converting it to qate objects | `hase` |
| Where a file lives, or whether a day is complete | `komachi` |
| Replay scheduling, batching, evaluation, the CLI | here |
| The viewer's pages and the API that serves run folders | here, `web.py` and `web/` |
| A strategy's dashboard, or its link from the viewer | the strategy's repository, and the `ENOSHIMA_API` env |

The example strategy is documentation. Keep it the most ordinary thing that works
and keep it honest about losing money; it is there to show the contract, not an
idea.

## Before publishing

```bash
pytest
python3 -m build && cd dist && unzip -q *.whl
grep -rniE 'AKIA|s3://|BEGIN [A-Z ]*PRIVATE KEY|/home/|api[_-]?key *=' .
```

The scan is on the wheel: what ships is the wheel. `k2-exchanges`' AGENTS.md
carries the list of private ecosystem names to add to the pattern — that list
cannot live in a public file.

The failure this catches is usually not a leaked credential. It is a public
package written as though its reader works here: a reference to a repository they
cannot open, or a dashboard they cannot load, explaining a decision they cannot
check.
