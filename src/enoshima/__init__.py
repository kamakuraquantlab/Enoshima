"""Enoshima — backtesting over recorded market data.

Replays the Kamakura Quant Lab bronze layer through a strategy and a simulated
exchange, and writes what happened to disk.

| Module | Holds |
|---|---|
| `runner` | Which parameter sets to run, and one shared pass over the data for all of them |
| `data_source` | Chunked replay: reads bronze a few days at a time, one copy shared |
| `backtest` | One strategy, one parameter set, one replay |
| `result` | Receiving a run's events and writing them locally |
| `eval` | Turning fills and PnL into Sharpe, drawdown, win rate |
| `run_log` | What a run recorded about itself, and why its id is deterministic |
| `influx` | The optional export of finished results into InfluxDB |
| `cli` | The `enoshima` command |

Everything sits on `qate`, which supplies the event loop, the strategy base class
and the simulator — and which cannot reach an exchange. A backtest here is
offline and deterministic by construction.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
