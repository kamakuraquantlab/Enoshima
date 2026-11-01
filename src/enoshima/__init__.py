"""Enoshima — backtesting over recorded market data.

Replays the Kamakura Quant Lab bronze layer through a strategy and a simulated
exchange, evaluates what happened, and writes it back to the data lake.

| Module | Holds |
|---|---|
| `runner` | Which parameter sets to run, and one shared pass over the data for all of them |
| `weekly` | Strict weekly runs: ISO weeks replayed alone, and any window built from them |
| `data_source` | Chunked replay: asks hase for a few days at a time, one copy shared |
| `backtest` | `BacktestRuntime`: one strategy, one replay, reported to its reporters |
| `result` | `ResultReporter`: the evaluations, the closed trades, and a run's summary |
| `ledger` | A closed trade, and the trades grouped by month, weekday, hour, holding time... |
| `lake` | Where results live: the lake's `research/` datasets, by env and run id, each part written whole |
| `records` | `RecordReporter`: fills, PnL updates, bars and metrics, kept for the lake |
| `eval` | Turning fills and PnL into Sharpe, drawdown, win rate |
| `run_log` | A run's provenance (its meta), and why its id is deterministic |
| `legacy` | Importing the run folders of an env written before results moved to the lake |
| `cli` | The `enoshima` command |
| `settings` | `enoshima.json`: fees, run sizing and weekly settings, per environment |
| `web` | `enoshima web`: the runs in the lake as a page, replaying nothing (the `web` extra) |

Market data comes from `hase.bronze`, already as qate's `Trade` and `OrderBook`;
hase in turn asks `komachi` which file holds a market-day. Enoshima reads no
file and asks komachi nothing. `qate` owns the model and the machinery: the event
loop, the strategy base class, the simulator — and no way to reach an exchange. A
backtest here is offline and deterministic by construction.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
