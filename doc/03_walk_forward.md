# Walk-forward: `enoshima walk-forward`

```bash
enoshima walk-forward --env NAME [--sum A+B]   # the table: every rule on the same hours
enoshima walk-forward --env NAME --train       # choose among the rules monthly, against no switching
```

[Selection](02_selection.md) recommends one parameter set per group. A
deployment does not have to hold that set forever: it could choose again every
so often, on the recent past. `walk-forward` asks whether that would have earned
more, and `--train` asks it the way a deployment would. Like `select`, it reads
the [weekly run](01_weekly_run.md) folders on disk and replays nothing. The
module docstring (`enoshima.post_analysis.walk_forward`) is the reference, and
this page is the map.

## The idea

A **rule** is three values:

| | meaning | example |
|---|---|---|
| `window` | `"rolling"`: choose on the latest `history`; `"expanding"`: on everything since the range began | `rolling` |
| `history` (X) | how much past the choice is made on | `5d` |
| `every` (Y) | how often the choice is made again; `never` chooses once and holds | `2d` |

At each choice the rule takes the group's runs, cuts each run's `trades.csv` to
the X hours before, and ranks them with `selection.rank`. That is the same
filters, score and grid-neighbour smoothing that `select` uses
([02_selection.md](02_selection.md)), with the env's own `"selection"`
settings, and there is no second way of choosing. It then trades the
winner for the next Y hours. Chained together, those choices make the rule's
**switching** curve, built entirely out of sample.

**No switching** is one parameter set held throughout. It is the bar switching
has to clear: if choosing again on the recent past does not beat holding a
set, the switching adds slippage and risk for nothing.

Nothing is replayed. A window's figures come from the run folders already on
disk, and a trade belongs to the JST hour it closed in, so any split on an
hour is exact.

## Settings (`enoshima.json`)

The rules are `"walk_forward"`'s, not flags. Without it, the default is one
history, `{"6m": ["1m", "never"]}`, with an expanding window. A grid of X and Y
to try looks like this:

```json
{
  "selection": {
    "group_by": null
  },
  "walk_forward": {
    "window": ["rolling"],
    "history": {
      "3d": ["4h", "12h", "1d", "2d", "3d"],
      "5d": ["12h", "1d", "2d", "3d", "4d"],
      "1w": ["12h", "1d", "2d", "3d", "1w"],
      "2w": ["1d", "3d", "1w", "2w"],
      "1m": ["1d", "1w", "2w"]
    },
    "test_from": null,
    "train_window": "expanding",
    "train_history": "1m",
    "retrain": "1m",
    "baseline": "default",
    "block_days": 5,
    "resamples": 10000
  }
}
```

- **X and Y are one map, not two axes.** `history` maps each history to its
  own list of `every`. An interval only makes sense against a history: choosing
  again every 4h is reasonable on three days of history and is noise on two
  months. The rules are `window × (history, every)` for each listed pair
  (`walk_forward.rules`). Two rules whose spans are the same length in hours
  are refused, so list only one of `1w` and `7d`.
- **Spans** are `h`, JST days `d`, weeks `w`, or calendar months `m`, and
  every boundary falls on a JST hour.
- **Groups come from `selection.group_by`**, as in
  [selection](02_selection.md#which-runs-in-which-groups). `null` means every
  categorical parameter of `strategy_param_grid.json`. Each group (for example one
  `schedule_version`) is trained alone, on its own runs, as `best.recommend`
  groups them. A rule only ever switches among the sets of one group.
- `test_from` is the first test hour of the plain table. By default it is the
  range start plus the longest history.
- `train_window`, `train_history` and `retrain` are `--train`'s outer loop:
  every `retrain`, rank the rules on the `train_history` before.
- `baseline` sets which set is held when there is no switching. `"default"` is
  the grid's `default_value`s, with the group's own values for the grouped
  parameters. `"once"` is the set chosen once at the start of the test. A
  default set that was never run falls back to `"once"`.
- `block_days` and `resamples` configure the significance test.

Unknown keys are refused, as everywhere in `enoshima.json`.

## Two commands

### `enoshima walk-forward --env NAME [--sum A+B]`: the table

Every rule is tested **on the same hours**, from `test_from` to the end, so
their totals compare. An expanding rule's history only sets where its test may
start, so expanding rules are collapsed to the longest history
(`on_common_hours`). `hindsight` is the set chosen on the whole range, scored
on the same hours. It has seen them, so it is a ceiling, not a rule.
`--sum A+B` adds groups traded side by side, such as two schedules that split
the day. The table goes to `env.log` and to
`post_analysis/walk_forward_<stamp>.json` (and `.txt`).

Trying many rules on the same test hours is itself a choice made in sample.
Prefer the rule that holds up across groups and environments over the one with
the best total in one.

### `enoshima walk-forward --env NAME --train`: training the rule

1. **Each rule is a candidate**, and so is the no-switching baseline. Each
   rule starts as soon as its own history allows: `rule_start = range start +
   history`. Do not align the rules on the longest history: doing so once held
   a 3d rule back by two months.
2. **The outer test** starts `train_history` after the earliest rule
   (`run_train` → `_tasks(..., earliest=True)`).
3. **Every `retrain`**, `pick(t)` ranks the candidates on the `train_history`
   before `t` with `selection.rank`. A rule's history and interval, in hours,
   are its grid axes, so neighbour smoothing works across rules. Only rules
   that traded the whole window are eligible (`advance(c["from"],
   train_history) <= t`). An expanding `train_window` starts at the latest
   eligible start, so all candidates are ranked on the same hours. The winner
   is traded until the next retraining. When no candidate passes the filters,
   the baseline is traded.
4. **The trained curve** never sees its own test hours, so it is compared with
   the baseline directly. The daily pnl5 and pnl10 differences are tested with
   a moving-block bootstrap (one-sided p-value), and the retrainings won are
   tested with a sign test.
5. **`current`** in `summary.json` is the rule chosen on the latest window: the
   one to trade now.

### Against each run (`runs.json`)

`against_each_run` scores every rule's switching curve against **every run of
the group held**, over that rule's own hours (`from` → `to`). It answers the
question a results viewer asks: "had I held this run's set instead, would
switching have done better?" `best` is the rule with the highest switching
pnl5.

## Output: `post_analysis/train/<train_id>/`

The folder is written whole (`write_training`, `.tmp-<pid>` then renamed),
with `summary.json` last. A viewer may read these files, so add to them rather
than rename them.

| file | holds |
|---|---|
| `meta.json` | env, strategy, range, `group`, `runs`, `inner_from`, `test`, `baseline`, `rules` (each with its `from`), `settings` |
| `folds.json` | each retraining: train and test hours, the chosen rule, its and the baseline's test totals, every candidate's `ranking` |
| `switches.json` | each span of the trained curve: the rule, `run_id` and parameter set held |
| `runs.json` | `{best, rules: [rule, from, to, totals], runs: {run_id: {params, rules: {key: totals held}}}}` |
| `curve.csv` | hourly `ts,series,trades,pnl0,pnl5,pnl10`: each rule from its own `from`; trained, baseline and hindsight from the outer test start |
| `summary.json` | totals of trained, baseline, hindsight and each rule; `significance`; `current` |

## Reading the results honestly

- **Only the trained-versus-baseline comparison is out of sample.** A best rule
  picked from the table, or from `runs.json`, is partly the luckiest of many.
  Picking among training settings by their p-values is the same fit one level
  up.
- **Measure against a no-skill switcher, not against 50%.** A switcher that
  picks at random every Y can beat most of a group's runs by chance when the
  group's sets are mostly losers. Simulate it on the same groups before calling
  a rule good.
- **Prefer a rule that holds across groups and environments** to the one with
  the best total in one of them.
- **Hold out time.** Choose X and Y on the hours before a cut, then judge them
  only on the hours after. A rule that wins in the first part and also in the
  second is worth a forward test. A rule that only wins on the whole range is
  not.
- **Short histories chase noise.** A history of a day or two, or an interval of
  an hour or two, mostly picks whatever happened to win last and pays slippage
  on every change. Expect such rules to lose to holding a set.
