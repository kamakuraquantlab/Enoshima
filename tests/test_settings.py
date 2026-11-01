"""`enoshima.json`: how an environment is backtested, and the command that reads it."""

import json

import pytest

from enoshima.data_source import DEFAULT_CHUNK_DAYS
from enoshima.runner import MAX_CONCURRENCY
from enoshima.settings import SETTINGS_FILE, load_settings


def write(tmp_path, data) -> None:
    (tmp_path / SETTINGS_FILE).write_text(json.dumps(data))


def test_no_file_means_the_defaults(tmp_path):
    settings = load_settings(tmp_path)
    assert settings.chunk_days == DEFAULT_CHUNK_DAYS
    assert settings.max_concurrency == MAX_CONCURRENCY


def test_the_file_sizes_a_run(tmp_path):
    write(tmp_path, {"chunk_days": 1, "max_concurrency": 4})
    settings = load_settings(tmp_path)
    assert (settings.chunk_days, settings.max_concurrency) == (1, 4)


@pytest.mark.parametrize("influx", [{"token": "t", "write_url": "http://db:8086"}, {}, None])
def test_an_influx_entry_from_before_the_lake_is_ignored_with_a_warning(tmp_path, influx, caplog):
    """Every env written before results moved to the lake has one; refusing it would stop them all."""
    write(tmp_path, {"chunk_days": 1, "influx": influx})
    assert load_settings(tmp_path).chunk_days == 1
    assert not hasattr(load_settings(tmp_path), "influx")
    assert "influx" in caplog.text and "data lake" in caplog.text


@pytest.mark.parametrize("key", ["selection", "walk_forward"])
def test_choosing_settings_from_before_are_ignored_with_a_warning(tmp_path, key, caplog):
    """Enoshima no longer chooses a parameter set; an env that configured it still runs."""
    write(tmp_path, {"chunk_days": 1, key: {"history": {"1d": ["4h"]}, "anything": True}})
    assert load_settings(tmp_path).chunk_days == 1
    assert not hasattr(load_settings(tmp_path), key)
    assert key in caplog.text and "no longer chooses" in caplog.text


@pytest.mark.parametrize("data", [{"infux": {}}, {"weekly": {"preload": 1}}, {"chunk_day": 1}])
def test_a_misspelling_is_refused_not_ignored(tmp_path, data):
    """A misspelt key must not quietly leave a run with the defaults."""
    write(tmp_path, data)
    with pytest.raises(ValueError):
        load_settings(tmp_path)


def test_the_command_takes_no_location_or_recording_flags():
    """Data, output, variant and recording come from komachi and the env, not flags."""
    from enoshima.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["run", "--env", "SOME_ENV", "--from", "2026-05-01"])
    assert args.chunk_days is None and args.max_concurrency is None
    for flag in ("--data-root", "--output-root", "--variant", "--influx"):
        with pytest.raises(SystemExit):
            parser.parse_args(["run", "--env", "SOME_ENV", "--from", "2026-05-01", flag, "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["markets"])


def test_strategy_logs_default_to_warning_and_can_be_raised(tmp_path):
    assert load_settings(tmp_path).strategy_log_level == "WARNING"
    (tmp_path / SETTINGS_FILE).write_text(json.dumps({"strategy_log_level": "INFO"}))
    assert load_settings(tmp_path).strategy_log_level == "INFO"


def test_a_strategy_log_level_that_is_not_one_is_refused(tmp_path):
    (tmp_path / SETTINGS_FILE).write_text(json.dumps({"strategy_log_level": "LOUD"}))
    with pytest.raises(ValueError, match="not a logging level"):
        load_settings(tmp_path)


def test_quieting_sets_levels_on_the_strategy_and_qate_trading_only():
    import logging

    from enoshima.cli import quiet_strategy_logs

    names = ("example", "qate.trading", "enoshima", "qate.core")
    before = {n: logging.getLogger(n).level for n in names}
    try:
        quiet_strategy_logs("example.strategy", "WARNING")
        assert logging.getLogger("example").level == logging.WARNING
        assert logging.getLogger("qate.trading.strategy").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("enoshima").level == before["enoshima"]
        assert logging.getLogger("qate.core").level == before["qate.core"]
    finally:
        for n, level in before.items():
            logging.getLogger(n).setLevel(level)


def test_weekly_sets_the_preload_and_overlap_and_refuses_unknown_keys(tmp_path):
    weekly = load_settings(tmp_path).weekly
    assert (weekly.preload_days, weekly.overlap_days) == (1, 1)
    write(tmp_path, {"weekly": {"preload_days": 2, "overlap_days": 3}})
    weekly = load_settings(tmp_path).weekly
    assert (weekly.preload_days, weekly.overlap_days) == (2, 3)
    write(tmp_path, {"weekly": {"preload_hours": 24}})   # what it was called before days
    with pytest.raises(ValueError, match="Unknown key"):
        load_settings(tmp_path)
