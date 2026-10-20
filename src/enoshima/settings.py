"""How an environment is backtested: `enoshima.json`, beside the env's other files.

```json
{
  "influx": {
    "token": "",
    "write_url": "http://localhost:8086",
    "read_url": "http://localhost:8086",
    "org": null
  },
  "chunk_days": 2,
  "max_concurrency": 16,
  "strategy_log_level": "WARNING",
  "selection": {
    "positive_pnl10": true,
    "min_trades": 0,
    "max_drawdown": null,
    "score": "balanced",
    "top_share": 0.05,
    "neighbors": true,
    "window": "expanding",
    "group_by": null,
    "train_months": 6,
    "test_months": 1
  },
  "walk_forward": {
    "window": ["expanding"],
    "history": {"6m": ["1m", "never"]},
    "test_from": null,
    "train_window": "expanding",
    "train_history": "1m",
    "retrain": "1m",
    "baseline": "default",
    "block_days": 5,
    "resamples": 10000
  },
  "weekly": {
    "preload_days": 1,
    "overlap_days": 1
  }
}
```

Every key is optional, and a missing file means the defaults.

- `influx` turns on recording to InfluxDB. Absent or `null`, a run records
  nothing and still has its result. `token`, `write_url` and `read_url` reach
  the server, by default a local one with no token; `org` defaults to the only
  one the token sees. The token lives here rather than among the credentials
  because it is not one an outside service issued: the InfluxDB is ours, set up
  for these runs, and the env says which one it charts to.
- `chunk_days` is how many days are loaded at once (memory).
- `max_concurrency` is how many parameter sets share one pass over the data.
- `strategy_log_level` is the level of the strategy's own loggers -- its
  package's, and `qate.trading`'s -- during a run. A live run logs every fill at
  INFO, which is what you want when one fails; a sweep would write that line for
  every fill of every parameter set (1.4 million lines, 600 MB, in one 45-way
  sweep). `"INFO"` brings it back when a backtest needs debugging.
- `selection` is how `enoshima select` chooses: filters, the score, grid-neighbour
  smoothing and the walk-forward window. See `enoshima.selection`.
- `walk_forward` is what `enoshima walk-forward` tries: the rules (window,
  and each history with its own reselection intervals, in hours, days, weeks
  or months), and for
  `--train` how often a rule is chosen again, on how much history, against
  which baseline. See `enoshima.post_analysis.walk_forward`.
- `weekly` is how `enoshima run --weekly` replays a week: `preload_days`
  before its Monday, to warm the strategy up, and `overlap_days` after its
  Sunday, to close the trades the week opened. A trade is the week's if it
  opened in it. Both are part of a weekly run's id, so a different preload or
  overlap starts a different set of weeks. See `enoshima.weekly`.

This is the environment's file rather than a set of flags because it describes
the environment: where its runs are charted and how big its data is. An unknown
key is refused rather than ignored, so a misspelt `influx` cannot quietly leave a
run unrecorded.
"""

import json
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path

from .data_source import DEFAULT_CHUNK_DAYS
from .post_analysis.walk_forward import WalkForwardSettings
from .runner import MAX_CONCURRENCY
from .selection import SelectionSettings
from .weekly import WeeklySettings

SETTINGS_FILE = "enoshima.json"


@dataclass
class InfluxSettings:
    token: str = ""
    write_url: str = "http://localhost:8086"
    read_url: str = "http://localhost:8086"
    org: str | None = None


@dataclass
class Settings:
    chunk_days: int = DEFAULT_CHUNK_DAYS
    max_concurrency: int = MAX_CONCURRENCY
    strategy_log_level: str = "WARNING"
    influx: InfluxSettings | None = None
    selection: SelectionSettings = field(default_factory=SelectionSettings)
    walk_forward: WalkForwardSettings = field(default_factory=WalkForwardSettings)
    weekly: WeeklySettings = field(default_factory=WeeklySettings)

    def __post_init__(self):
        if not isinstance(logging.getLevelName(self.strategy_log_level), int):
            raise ValueError(f"strategy_log_level {self.strategy_log_level!r} is not a logging level, e.g. WARNING")


def _check_keys(data: dict, cls, where: str) -> None:
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"Unknown key(s) {sorted(unknown)} in {where}")


def load_settings(work_dir: str | Path) -> Settings:
    path = Path(work_dir) / SETTINGS_FILE
    if not path.is_file():
        return Settings()
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    _check_keys(data, Settings, str(path))

    influx = data.pop("influx", None)
    if influx is not None:
        if not isinstance(influx, dict):
            raise ValueError(f'"influx" in {path} must be an object, e.g. {{"token": "..."}}')
        _check_keys(influx, InfluxSettings, f'"influx" in {path}')
        influx = InfluxSettings(**influx)

    sections = {}
    for name, cls in (("selection", SelectionSettings), ("walk_forward", WalkForwardSettings),
                      ("weekly", WeeklySettings)):
        section = data.pop(name, None)
        if section is None:
            section = cls()
        else:
            if not isinstance(section, dict):
                raise ValueError(f'"{name}" in {path} must be an object')
            _check_keys(section, cls, f'"{name}" in {path}')
            section = cls(**section)
        sections[name] = section
    return Settings(**data, influx=influx, **sections)
