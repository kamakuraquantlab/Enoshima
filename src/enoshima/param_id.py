"""The string that names a run's parameter set: its InfluxDB bucket and its run log's tag.

Built here from `param_grid.json`, not by the strategy. It used to be each
strategy's `get_param_set_id`, which made every strategy carry code that has
nothing to do with trading, and made the bucket a run wrote to depend on which
override a variant happened to inherit.

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
another, renamed every bucket when a factor changed, and fell silently to 1 when
one was missing or misspelt. A bucket name may contain a dot, so it went.

The grid gives only the order, so that the same values give the same id whether
they came from `params.json`, the grid or `params_combo.json`. Parameters the grid
does not name follow, in the order the parameter set gives them. An environment
with no grid gets that for every parameter. Widening a sweep does not rename the
buckets of the combinations already run; reordering the grid does.

## Temporary runs

A temporary run appends `_tmp_<timestamp>`, so it writes a bucket of its own
rather than into the one a parameter set's permanent results accumulate in.
"""

from decimal import Decimal

TMP_MARKER = "_tmp_"


def param_set_id(env_name: str, params: dict, param_grid: list[dict] | None, tmp_ts: int | None = None) -> str:
    """`tmp_ts`, a unix timestamp, marks the run temporary."""
    grid_names = [spec["name"] for spec in param_grid or []]
    names = [name for name in grid_names if name in params]
    names += [name for name in params if name not in grid_names]
    psid = "_".join([env_name] + [_segment(params[name]) for name in names])
    if tmp_ts is None:
        return psid
    return f"{psid}{TMP_MARKER}{tmp_ts}"


def _segment(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        # repr is the shortest string that round-trips; Decimal drops its exponent.
        return format(Decimal(repr(value)), "f")
    return str(value)
