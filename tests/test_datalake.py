from pathlib import Path

import pyarrow.parquet as pq
from qate.core.model import ExchangeName, Measurement, Metric, Tag
from qate.core.symbol import Symbol
from qate.util.dt_range import DtRange

from enoshima.datalake import DatalakeReporter


def bar(ts: float, bar_ts: float, close: float) -> Metric:
    return Metric(
        Measurement.BAR.value,
        ts,
        [
            Tag.EXCHANGE_NAME.value,
            ExchangeName.BINANCE.value,
            Tag.SYMBOL.value,
            Symbol.BTC_USDT.value,
        ],
        [
            "ts",
            bar_ts,
            "open",
            close - 1,
            "high",
            close + 2,
            "low",
            close - 2,
            "close",
            close,
            "sar:up",
            True,
            "sar:s",
            close - 10,
            "sar:ns",
            close - 9,
        ],
    )


def test_bar_metrics_use_the_historical_backtest_bar_layout(tmp_path: Path):
    dt_range = DtRange.from_strings("20260102", "20260102")
    reporter = DatalakeReporter(tmp_path, "example.strategy", "Q_0.002_0.02", dt_range)

    reporter.report_metrics(
        [
            bar(dt_range.start_ts - 1, dt_range.start_ts - 2, 99.0),  # warmup: not saved
            Metric(Measurement.TRADING.value, dt_range.start_ts, [], ["ignored", 1]),
            bar(dt_range.start_ts + 10, dt_range.start_ts + 9, 100.0),
            bar(dt_range.start_ts + 20, dt_range.start_ts + 19, 101.0),
        ]
    )
    reporter.stop()

    path = (
        tmp_path
        / "research/dataset=Backtest_Bar/strategy=example.strategy/exchange=BINANCE"
        / "symbol=BTC_USDT/param_id=Q_0.002_0.02/date=2026-01-02/data.parquet"
    )
    assert path.is_file()

    table = pq.ParquetFile(path).read()
    assert table.column_names == ["ts", "open", "high", "low", "close", "sar:up", "sar:s", "sar:ns"]
    frame = table.to_pandas()
    assert frame["ts"].tolist() == [dt_range.start_ts + 9, dt_range.start_ts + 19]
    assert frame["close"].tolist() == [100.0, 101.0]
    assert frame["sar:up"].tolist() == [True, True]


def test_stopping_without_bars_creates_nothing(tmp_path: Path):
    dt_range = DtRange.from_strings("20260102", "20260102")
    reporter = DatalakeReporter(tmp_path, "example.v1", "params", dt_range)
    reporter.stop()
    assert not (tmp_path / "research").exists()
