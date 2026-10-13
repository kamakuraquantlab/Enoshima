"""Enoshima — backtesting over recorded market data.

Replays the Kamakura Quant Lab bronze layer through a strategy and a simulated
exchange, evaluates what happened, and optionally records it in InfluxDB.

| Module | Holds |
|---|---|
| `runner` | Which parameter sets to run, and one shared pass over the data for all of them |
| `bronze` | Turning bronze parquet into qate events: the join between komachi and qate |
| `data_source` | Chunked replay: reads bronze a few days at a time, one copy shared |
| `backtest` | `BacktestRuntime`: one strategy, one replay, reported to its reporters |
| `result` | `ResultReporter`: the evaluations, a run's result, its PnL and bars |
| `timeseries` | The Hive-partitioned parquet a run's PnL and bars are written to |
| `eval` | Turning fills and PnL into Sharpe, drawdown, win rate |
| `run_log` | What a run recorded about itself, and why its id is deterministic |
| `influx` | `InfluxReporter`: the optional metric stream, written to InfluxDB as it runs |
| `cli` | The `enoshima` command |
| `settings` | `enoshima.json`: InfluxDB recording and run sizing, per environment |

Enoshima is the join between two packages that know nothing about each other.
`komachi` owns the data: it writes the bronze layer, so it is asked where a file
is and which days are complete. `qate` owns the model and the machinery: the event
loop, the strategy base class, the simulator — and no way to reach an exchange. A
backtest here is offline and deterministic by construction.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
