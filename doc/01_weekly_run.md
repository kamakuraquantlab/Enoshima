# The weekly run

```bash
enoshima run --env NAME --from 2026-01-01 --to 2026-12-31 --optimize grid --weekly
enoshima run --env NAME --from 2026-01-01 --to 2027-01-31 --optimize grid --weekly   # replays only January's weeks
```

This is how Enoshima is mainly used, and the only kind of run that
[selection](02_selection.md) and [walk-forward](03_walk_forward.md) read. The
module docstring (`enoshima.weekly`) is the reference.

## The idea: a backtest that accumulates

A backtest of a year, re-run a month later over thirteen months, should not
replay the year again. A weekly run computes a backtest one week at a time.
Each week's result depends only on

    the env, the strategy and its revision, the parameters, the preload, the overlap, the week

So a week computed once is the week every later run needs. It is skipped rather
than replayed, and a longer backtest is its weeks put together. An extended run
is identical, to the byte, to the same run made at once.

## One week's replay

The requested range is widened to the ISO weeks that cover it, Monday 00:00 to
Sunday 24:00 JST. Each week is replayed on its own, with a fresh strategy and a
fresh simulated exchange, fed `preload_days` before the Monday and
`overlap_days` after the Sunday (`enoshima.json`'s `"weekly"`, 1 and 1 by
default). For 2026-W41, 10-05 to 10-11:

    10-04          10-05 ... 10-11          10-12
    preload        the week                 overlap
    warms up the   every trade opened       closes what the week opened;
    strategy       here is the week's       its own opens are the next week's

- **Preload.** The strategy's warmup and first trades. A trade opened in the
  preload belongs to the previous week, which counts it there, so it is not
  counted here.
- **The week.** Every position *opened* from Monday to Sunday is the week's,
  wherever it closes.
- **Overlap.** It is there to see the week's positions closed. It is the next
  week's own Monday, so a trade opened in it is the next week's. An open order
  made in the overlap is accepted and never filled (`OverlapGateway`). If it
  were filled, it could add to a position the week opened and move that
  position's open time into the next week.
- **Stop.** The replay ends as soon as the overlap has closed the week's last
  position.

### When a PnL is recorded

A trade's PnL is recorded when its position closes, and is counted in the week
the position *opened* in. Each trade is therefore counted by exactly one week.
The orders a week counts, behind the slippage averages and in InfluxDB, are its
own trades' legs, wherever they filled, so they also add up across weeks.

`by_week.json` groups by *close*, like every `by_*.json`. A trade opened on a
Sunday and closed on the Monday is in the next week's row there, and in its own
week's folder.

## As far as the data goes

Each week is clipped to the complete days on disk: from the first to the last
day that every stream the strategy reads has, less the overlap at the end. A
range asked for up to today runs the current week's opens to the day before
yesterday. A week with no such day is not run.

A clipped week records its own `start_str`/`end_str` and `"partial": true`. The
next weekly run whose data reaches further into it runs it again, so the last
week grows to full without `--force`. This is decided by `_weeks_to_run`, not
by the skip check a temporary run uses. A day missing *between* the first and
the last is not clipped: it replays as empty, with a warning. `--force` reruns
the weeks asked for.

## What it gives up

It does not promise equality with one continuous replay. Each week starts cold:

- a position opened before the preload and still open on the Monday is in
  neither week;
- a strategy whose state needs more history than the preload behaves
  differently on Mondays;
- a trade the overlap did not see closed is in no week.

Make the preload longer than the strategy's memory, and the overlap longer than
its holding time. Each week's `summary.json` has `open_at_end`, the positions
the week opened that were still held when its replay ended.

## How the result is saved

One folder per environment, strategy revision, parameter set, preload and
overlap. There are no dates in its id, so every weekly run of them adds to it:

```text
run_log/<run_id>/
├── meta.json, summary.json,      rebuilt from every week on disk: an ordinary
│   trades.csv, by_*.json         run over the first Monday .. the last Sunday
└── weeks/<YYYY-Www>/             what a run writes, one week at a time
    ├── meta.json                 the week's provenance: batch, bucket, timings, partial
    ├── summary.json              the week's figures, plus order counters and open_at_end
    └── trades.csv
```

- **Each week is written whole**, into a temporary folder renamed into place.
- **The root files are never the current run's result.** They are rebuilt
  (`weekly.rebuild`) from all the weeks on disk, over the first week's start to
  the last week's end (a partial week's own end). A weekly root therefore looks
  like any other run folder, and `results`, `select` and `walk-forward` read it
  unchanged.
- **No gaps.** To keep the root's range true, a run also replays the weeks
  between those on disk and those asked for: with weeks 1–10 on disk and week
  30 asked for, it replays 11–30. A root whose weeks have a gap (a week that
  failed) has no `summary.json` until a run fills it, and an unfinished folder
  is not read.
- **A new strategy revision is a new folder**, so weeks of two versions of the
  code are never put together.
- **InfluxDB**, if configured, gets one bucket for the run,
  `backtest_<param_set_id>_w<run_id>`, with every point tagged `week=<YYYY-Www>`.
  A week's points are deleted by that tag before the week is written again.
  The delete goes by tag rather than by time because a Sunday open has its
  close-aligned copy on the Monday.

The root's `trades.csv` and `by_month.json` are what
[selection](02_selection.md) ranks and walks forward on, and
[walk-forward](03_walk_forward.md) cuts its hourly windows from the same
`trades.csv`. Neither replays anything.

## Checking it

`tests/test_weekly.py` checks on synthetic data that an extended run is the run
made at once. `tools/weekly_consistency.py` checks it on yours. It copies an env
twice, runs one copy in two steps and the other in one, and compares every
trade and figure of the two:

```bash
python tools/weekly_consistency.py --env NAME --from 2026-09-07 --split 2026-09-21 --to 2026-10-04
```
