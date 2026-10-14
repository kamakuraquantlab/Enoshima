"""The parameter set's id: what names its bucket, built from the grid, not the strategy."""

from enoshima.param_id import param_set_id

GRID = [
    {"name": "stop", "default_value": 0.04, "values": [0.02, 0.04]},
    {"name": "threshold", "default_value": 0.0015, "values": [0.0015, 0.002]},
]


def test_the_env_name_comes_first_and_stands_for_the_config():
    assert param_set_id("EXAMPLE_BACKTEST", {"stop": 0.04, "threshold": 0.0015}, GRID) == "EXAMPLE_BACKTEST_0.04_0.0015"


def test_values_follow_the_grid_order():
    # Given in the opposite order: the grid's is the one that counts.
    assert param_set_id("ENV", {"threshold": 0.0015, "stop": 0.04}, GRID) == "ENV_0.04_0.0015"


def test_a_small_value_is_a_plain_decimal_not_an_exponent():
    assert param_set_id("ENV", {"stop": 0.04, "threshold": 0.00001}, GRID) == "ENV_0.04_0.00001"


def test_a_whole_float_is_written_as_the_integer_it_equals():
    """10 in params.json and 10.0 in a grid are the same parameter set."""
    grid = [{"name": "barrier_bps", "default_value": 10, "values": [10]}]
    assert param_set_id("ENV", {"barrier_bps": 10.0}, grid) == param_set_id("ENV", {"barrier_bps": 10}, grid)


def test_params_the_grid_does_not_name_follow():
    params = {"mode": "2", "stop": 0.04, "threshold": 0.0015, "enabled": True}
    assert param_set_id("ENV", params, GRID) == "ENV_0.04_0.0015_2_true"


def test_no_grid_means_the_params_own_order():
    assert param_set_id("ENV", {"threshold": 3.5, "time_cap_s": 300}, None) == "ENV_3.5_300"


def test_widening_the_grid_does_not_rename_an_existing_set():
    wider = [dict(spec, values=spec["values"] + [0.0025]) for spec in GRID]
    params = {"stop": 0.04, "threshold": 0.0015}
    assert param_set_id("ENV", params, wider) == param_set_id("ENV", params, GRID)


def test_a_temporary_run_appends_its_timestamp():
    params = {"stop": 0.04, "threshold": 0.0015}
    assert param_set_id("ENV", params, GRID, tmp_ts=1760000000) == "ENV_0.04_0.0015_tmp_1760000000"
