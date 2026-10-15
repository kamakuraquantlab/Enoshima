"""Enoshima — backtesting over recorded market data.

Replays the Kamakura Quant Lab bronze layer through a strategy and a simulated
exchange, evaluates what happened, and optionally records it in InfluxDB.

| Module | Holds |
|---|---|
| `runner` | Which parameter sets to run, and one shared pass over the data for all of them |
| `data_source` | Chunked replay: asks hase for a few days at a time, one copy shared |
| `backtest` | `BacktestRuntime`: one strategy, one replay, reported to its reporters |
| `result` | `ResultReporter`: the evaluations, the closed trades, and a run's summary |
| `ledger` | A closed trade, and the trades grouped by month, weekday, hour, holding time... |
| `run_dir` | A run's folder: meta, summary, trades and groupings, written whole or not at all |
| `eval` | Turning fills and PnL into Sharpe, drawdown, win rate |
| `run_log` | A run's provenance (`meta.json`), and why its id is deterministic |
| `selection` | How `--optimize` chooses: filters, grid neighbours, walk-forward months |
| `influx` | `InfluxReporter`: the optional metric stream, written to InfluxDB as it runs |
| `cli` | The `enoshima` command |
| `settings` | `enoshima.json`: InfluxDB recording and run sizing, per environment |

Market data comes from `hase.bronze`, already as qate's `Trade` and `OrderBook`;
hase in turn asks `komachi` which file holds a market-day. Enoshima reads no
file and asks komachi nothing. `qate` owns the model and the machinery: the event
loop, the strategy base class, the simulator — and no way to reach an exchange. A
backtest here is offline and deterministic by construction.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
