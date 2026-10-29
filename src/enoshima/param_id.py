"""The string that names a run's parameter set, readably: recorded in its meta, for finding it by.

Built here from `strategy_param_grid.json`, not by the strategy. It used to be each
strategy's `get_param_set_id`, which made every strategy carry code that has
nothing to do with trading, and made the name a run was recorded under depend on
which override a variant happened to inherit.

## How it is built

The environment's name, then one segment per parameter, joined with `_`, in the
grid's order:

    [{"name": "threshold", ...}, {"name": "period", ...}]
    {"threshold": 0.0015, "period": 60}  ->  "EXAMPLE_BACKTEST_0.0015_60"

Nothing else about the run is in it. An environment's config -- the market, the
order size, the strategy and variant -- is fixed, so the environment's name already
stands for all of it, and within one environment only the parameters vary.

Each value is written as itself, as a plain decimal: `0.00001`, never `1e-05`, and
`10.0` as `10`. Grid specs once carried a `"factor"` to scale values into integers
and keep the id free of dots; that made `200` mean 0.002 in one place and 0.2 in
another, renamed every InfluxDB bucket when a factor changed, and fell silently to 1 when
one was missing or misspelt. A bucket name may contain a dot, so it went.

The grid gives only the order, so that the same values give the same id whether
they came from the grid defaults or an expanded combination. Parameters the grid
does not name follow, in the order the parameter set gives them. An environment
with no grid gets that for every parameter. Widening a sweep does not rename the
ids of the combinations already run; reordering the grid does.

It is recorded in each run's meta and the lake's `BacktestRun`, for a reader to
find a parameter set by; the run id, not this, is what partitions the files.
"""

from decimal import Decimal


def param_set_id(env_name: str, params: dict, param_grid: list[dict] | None) -> str:
    grid_names = [spec["name"] for spec in param_grid or []]
    names = [name for name in grid_names if name in params]
    names += [name for name in params if name not in grid_names]
    return "_".join([env_name] + [_segment(params[name]) for name in names])


def _segment(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        # repr is the shortest string that round-trips; Decimal drops its exponent.
        return format(Decimal(repr(value)), "f")
    return str(value)
