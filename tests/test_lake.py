"""The lake's backtest datasets: written whole by part, read back exactly, and imported from run folders."""

import csv
import json

import pyarrow.parquet as pq
import pytest
from qate.core.model import ExchangeName, Measurement, Metric, Tag
from qate.core.symbol import Symbol
from qate.util.dt_range import DtRange

from enoshima import lake, legacy
from enoshima.lake import WHOLE, Store
from enoshima.ledger import TRADE_COLUMNS, Trade
from enoshima.records import TAGS_KEY, RecordReporter

RANGE = DtRange.from_strings("2026-01-20", "2026-01-21")


def trade(ts: float, change: float) -> Trade:
    return Trade(ts, ts - 60, "GMO", "BTC_JPY", "long", 100.0, 100.0, change, 0.1, change, change, change, 1.5)


def bar(ts: float, bar_ts: float, close: float) -> Metric:
    return Metric(
        Measurement.BAR.value,
        ts,
        [Tag.EXCHANGE_NAME.value, ExchangeName.BINANCE.value, Tag.SYMBOL.value, Symbol.BTC_USDT.value],
        ["ts", bar_ts, "close", close, "sar:up", True, "sar:s", close - 10],
    )


def test_a_part_is_finished_only_once_its_row_is_written_and_a_rewrite_replaces_it(tmp_path):
    store = Store(tmp_path, "ENV")
    t = RANGE.start_ts + 3600
    store.write_part("abc", WHOLE, {"id": "abc", "params": {"x": 1}}, {"pnl0": 1.0, "pnl_cnt0": 1}, [trade(t, 1.0)])
    assert store.parts("abc") == [WHOLE] and store.run_ids() == []   # no run row yet
    store.write_run("abc", {"id": "abc", "params": {"x": 1}}, {"pnl0": 1.0, "pnl_cnt0": 1}, [trade(t, 1.0)])
    assert store.run_ids() == ["abc"]
    run = store.read_run("abc")
    assert run["meta"] == {"id": "abc", "params": {"x": 1}} and run["summary"] == {"pnl0": 1.0, "pnl_cnt0": 1}
    assert store.trades("abc") == [trade(t, 1.0)]
    assert store.grouping("abc", "month") == {"2026-01": store.grouping("abc", "month")["2026-01"]}
    assert store.grouping("abc", "month")["2026-01"]["trades"] == 1

    # Writing a part again takes the run's row away, and leaves no file of the earlier write.
    store.write_part("abc", WHOLE, {"id": "abc"}, {"pnl0": 0.0}, [])
    assert store.run_ids() == []
    assert not store.path(lake.TRADE, "abc", WHOLE).exists()
    assert store.read_part_trades("abc", WHOLE) == []
    assert not list(tmp_path.rglob("*.tmp-*"))


def test_the_layout_is_the_research_layer_by_env_run_and_part(tmp_path):
    store = Store(tmp_path, "ENV")
    assert store.path(lake.TRADE, "abc", "2026-W04") == (
        tmp_path / "research" / "dataset=BacktestTrade" / "env=ENV" / "run_id=abc" / "part=2026-W04" / "data.parquet")
    assert store.path(lake.RUN, "abc") == tmp_path / "research/dataset=BacktestRun/env=ENV/run_id=abc/data.parquet"
    store.write_part("abc", WHOLE, {}, {}, [])
    store.write_run("abc", {}, {}, [])
    Store(tmp_path, "OTHER").write_part("x", WHOLE, {}, {}, [])   # parts only: not a finished run
    assert lake.envs(tmp_path) == ["ENV"]


def test_the_trades_file_has_the_ledgers_columns_in_order(tmp_path):
    store = Store(tmp_path, "ENV")
    store.write_part("abc", WHOLE, {}, {}, [trade(RANGE.start_ts + 1, 1.0)])
    table = pq.ParquetFile(store.path(lake.TRADE, "abc", WHOLE)).read()
    assert table.column_names == TRADE_COLUMNS


def test_a_run_rows_columns_are_its_meta_and_summary(tmp_path):
    store = Store(tmp_path, "ENV")
    meta = {"strategy": "s.v1", "params": {"x": 0.5}, "is_temporary_run": False, "strategy_commit": None}
    store.write_run("abc", meta, {"pnl5": 1.5, "win_avg5": None, "pnl_cnt5": 3}, [])
    row = pq.ParquetFile(store.path(lake.RUN, "abc")).read().to_pylist()[0]
    assert row["strategy"] == "s.v1" and row["is_temporary_run"] is False and row["strategy_commit"] is None
    assert json.loads(row["params"]) == {"x": 0.5}
    assert (row["pnl5"], row["win_avg5"], row["pnl_cnt5"]) == (1.5, None, 3)
    assert json.loads(row["meta"]) == meta


def test_a_finished_part_missing_a_file_is_refused(tmp_path):
    store = Store(tmp_path, "ENV")
    store.write_part("abc", "2026-W04", {}, {}, [trade(RANGE.start_ts + 1, 1.0)])
    store.path(lake.TRADE, "abc", "2026-W04").unlink()
    with pytest.raises(lake.IncompletePart, match="BacktestTrade"):
        store.trades("abc")


def test_records_are_kept_by_dataset_with_names_not_enum_values():
    reporter = RecordReporter(RANGE)
    inside, before = RANGE.start_ts + 600, RANGE.start_ts - 600
    reporter.report_metrics([bar(inside, inside - 60, 100.0), bar(before, before - 60, 90.0)])
    reporter.report_metrics([Metric(Measurement.MARKET_PRICE.value, inside, [], ["BID", 1.0])])   # bronze's
    reporter.report_metrics([Metric(Measurement.SYS.value, inside, ["desc", "x"], ["cpu", 0.5])])
    tables = reporter.tables()

    # A bar is at its own time, keeps its fields' names, and the warmup's is not kept.
    assert tables[lake.BAR].to_pylist() == [{"ts": inside - 60, "EXCHANGE_NAME": "BINANCE", "SYMBOL": "BTC_USDT",
                                             "close": 100.0, "sar:up": True, "sar:s": 90.0}]
    assert tables[lake.METRIC].to_pylist() == [{"ts": inside, "measurement": "SYS", "desc": "x", "cpu": 0.5}]
    assert tables[lake.ORDER] is None and tables[lake.PNL] is None
    # Which columns were tags is the file's, for a loader into a database with tags.
    assert json.loads(tables[lake.BAR].schema.metadata[TAGS_KEY]) == ["EXCHANGE_NAME", "SYMBOL"]
    assert json.loads(tables[lake.METRIC].schema.metadata[TAGS_KEY]) == ["desc"]


def test_columns_first_seen_late_are_padded():
    reporter = RecordReporter(RANGE)
    t = RANGE.start_ts + 1
    reporter.report_metrics([Metric(Measurement.SYS.value, t, [], ["a", 1.0]),
                             Metric(Measurement.SYS.value, t + 1, [], ["b", 2.0])])
    assert reporter.tables()[lake.METRIC].to_pylist() == [
        {"ts": t, "measurement": "SYS", "a": 1.0, "b": None},
        {"ts": t + 1, "measurement": "SYS", "a": None, "b": 2.0},
    ]


def write_folder(path, meta, summary, trades):
    path.mkdir(parents=True)
    (path / "meta.json").write_text(json.dumps(meta))
    (path / "summary.json").write_text(json.dumps(summary))
    with open(path / "trades.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(TRADE_COLUMNS)
        writer.writerows([[getattr(t, c) for c in TRADE_COLUMNS] for t in trades])


def test_run_folders_from_before_the_lake_are_imported(tmp_path):
    work_dir, store = tmp_path / "env", Store(tmp_path / "lake", "ENV")
    t = RANGE.start_ts + 1
    write_folder(work_dir / "run_log/tmp1", {"id": "tmp1", "is_temporary_run": True}, {"pnl5": 1.0}, [trade(t, 1.0)])
    weekly = work_dir / "run_log/wk1"
    write_folder(weekly, {"id": "wk1", "mode": "weekly"}, {"pnl5": 3.0}, [trade(t, 1.0), trade(t + 7 * 86400, 2.0)])
    write_folder(weekly / "weeks/2026-W04", {"week": "2026-W04"}, {"pnl5": 1.0}, [trade(t, 1.0)])
    write_folder(weekly / "weeks/2026-W05", {"week": "2026-W05"}, {"pnl5": 2.0}, [trade(t + 7 * 86400, 2.0)])
    write_folder(work_dir / "run_log/old.merged", {}, {}, [])   # set aside: not imported
    (work_dir / "best_result/7").mkdir(parents=True)   # an earlier version's choice: not imported
    (work_dir / "best_result/7/selection.json").write_text("{}")

    assert legacy.import_env(work_dir, store)["runs"] == 2   # a dry run writes nothing
    assert store.all_run_ids() == []

    report = legacy.import_env(work_dir, store, apply=True)
    assert (report["runs"], report["parts"]) == (2, 3)
    assert store.run_ids() == ["tmp1", "wk1"]
    assert store.parts("tmp1") == [WHOLE] and store.parts("wk1") == ["2026-W04", "2026-W05"]
    assert [x.pnl_change5 for x in store.trades("wk1")] == [1.0, 2.0]   # the weeks', not the root's copy
    assert store.read_run("wk1")["meta"] == {"id": "wk1", "mode": "weekly"}
    assert not list((tmp_path / "lake").rglob("selection.json"))
    # The folders are left; a second import finds everything in the lake.
    assert (work_dir / "run_log/wk1/trades.csv").is_file()
    assert legacy.import_env(work_dir, store, apply=True)["skipped"] == 2
