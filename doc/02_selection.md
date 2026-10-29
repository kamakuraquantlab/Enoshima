# Selection: `enoshima select`

```bash
enoshima select --env NAME
```

A run writes results and does not choose. `select` recommends a parameter set
from the [weekly run](01_weekly_run.md) folders already on disk, whenever you ask:
after a sweep, after extending it, or after changing the rule. It replays
nothing. The module docstrings (`enoshima.selection`, `enoshima.best`) are the
reference.

The set with the best whole-range PnL is the one that fit that range best. That
is not the same as the set most likely to keep working. Every step below pushes
against that.

## Which runs, in which groups

`select` reads every finished, non-temporary run of the env in the data lake. A run
without `--weekly` is a quick look and is never a candidate.

1. **By strategy, revision and range.** Runs of different variants, revisions
   or ranges are not measuring the same thing, and each such set gets its own
   recommendation. Every range on disk is recommended for, not only the latest.
2. **By `group_by`.** Each of those is split by the values of the `group_by`
   parameters. `null`, the default, means every categorical parameter in
   `strategy_param_grid.json` (a schedule, a mode). A categorical value is a
   different way of trading, not a point on a scale. It should be compared on
   its own merits, not outranked on the noisy figures that choose a box size.
   `[]` makes a whole range one group, and a list of names groups by exactly
   those.

Each group is ranked on its own, with neighbours taken from its own runs, and
gets its own recommendation.

## The rule, step by step

### 1. Filters

- `positive_pnl10`: a set that loses money at the pessimistic slippage ranks
  below every set that does not.
- `min_trades`: a set must trade at least this often.
- `max_drawdown`: pnl5's maximum drawdown must stay within this, in the quote
  currency.

A set that fails is ranked below every set that passes, whatever its PnL. A
group where no set passes gets no recommendation.

### 2. Score

With `"score": "balanced"`, four figures, each asking something the others do
not:

| Figure | Better | Asks |
|---|---|---|
| `pnl5` | higher | what it earns at the realistic slippage |
| `pnl10` | higher | whether it survives degraded execution |
| `dd5` | lower | how deep it falls in a period that does not suit it |
| `top_share5` | lower | how much of its winnings its best `top_share` (5%) of trades made |

Each figure becomes a percentile among the sets that pass the filters, 1 for
the best and 0 for the worst. The score is the unweighted mean of the four. It
uses ranks rather than a weighted sum because the figures are in different units,
and a weight would be one more parameter to fit. A set wins for being good on
all four, not exceptional on one.

`top_share5` is the pnl5 of the best `ceil(top_share × trades)` trades over the
pnl5 of all winning trades. A set whose profit is a handful of big wins depends
on the next range holding the same handful.

`"score": "pnl5"` ranks on pnl5 alone. It is kept for comparison. On three
trend-following sweeps it kept choosing the deepest drawdowns.

### 3. Neighbour smoothing

With `"neighbors": true`, each figure is averaged with the same figure of the
set's grid neighbours before it is ranked. The neighbours are the sets one step
away along one numeric parameter. A peak its neighbours do not share is likely
a fit to noise, and a plateau is not, so a lone spike loses to a plateau.
Categorical parameters have no order and so no neighbours. The neighbours are
the values the group's runs actually took, so an older range is judged by the
grid it was run on.

### 4. Walk-forward over months

Each run has its PnL by JST month (its `by_month` grouping) and its trades.
`select` makes its choice on the months before a test month and scores that
choice on the following `test_months`. It then steps forward and repeats. The
sum of the test months is what choosing this way would have earned out of
sample. That is the figure to trust, more than any in-sample best.

- `"window": "expanding"` (default): each choice uses every complete month since
  the range began. `train_months` is only the history the first test needs. The
  set recommended is chosen on the whole range.
- `"window": "rolling"`: each choice uses the latest `train_months`, and the
  recommendation is made on the most recent ones. On three trend-following
  sweeps a rolling six months chose on noise.
- `train_months: 0` turns the walk-forward off.

Only complete JST months inside the range are used.

This walk-forward tests *one* fixed rule. Whether to choose again more often, on
a different history, is a question for [walk-forward](03_walk_forward.md).

## Settings (`enoshima.json`)

```json
{
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
  }
}
```

| Key | Default | Is |
|---|---|---|
| `positive_pnl10` | `true` | filter out sets losing at pnl10 |
| `min_trades` | `0` | filter: minimum trades |
| `max_drawdown` | `null` | filter: maximum pnl5 drawdown, quote currency |
| `score` | `"balanced"` | `"balanced"` (four percentiles) or `"pnl5"` |
| `top_share` | `0.05` | the share of best trades in `top_share5`, in (0, 1) |
| `neighbors` | `true` | smooth each figure over grid neighbours |
| `window` | `"expanding"` | `"expanding"` or `"rolling"` |
| `train_months` | `6` | rolling: the window; expanding: the first test's history; `0` turns it off |
| `test_months` | `1` | months scored per step, and the step |
| `group_by` | `null` | parameter names to group by; `null` = the categorical ones, `[]` = none |

Unknown keys are refused. [walk-forward](03_walk_forward.md) uses the same
filters, score, neighbours and `group_by`.

## Output: `best_result/<batch_id>/`

In the env's analysis folder in the data lake, `research/enoshima/<env>/`.

| File | Holds |
|---|---|
| `selection.json` | per range and group: the ranking, the folds, the out-of-sample totals, the recommendation |
| `<run_id>.json` | each recommended run's meta |

A group where no set passes the filters has no copy, because nothing is
recommended for it.
