"""How an environment is backtested: `enoshima.json`, beside the env's other files.

```json
{
  "fee_rates": {
    "GMO:XRP_JPY": {"maker": 0.0, "taker": 0.0}
  },
  "chunk_days": 2,
  "max_concurrency": 16,
  "strategy_log_level": "WARNING",
  "weekly": {
    "preload_days": 1,
    "overlap_days": 1
  }
}
```

Every key is optional, and a missing file means the defaults.

- `fee_rates` is what each market charges a fill, as a fraction of notional
  (a negative maker rate is a rebate). Every market the strategy trades needs
  one, zero written out, or the run is refused before it starts: a backtest
  installs no venue package, so this is the only place a rate can come from.
  Non-zero rates are part of a run's id. See `enoshima.fees`.
- `chunk_days` is how many days are loaded at once (memory).
- `max_concurrency` is how many parameter sets share one pass over the data.
- `strategy_log_level` is the level of the strategy's own loggers -- its
  package's, and `qate.trading`'s -- during a run. A live run logs every fill at
  INFO, which is what you want when one fails; a sweep would write that line for
  every fill of every parameter set (1.4 million lines, 600 MB, in one 45-way
  sweep). `"INFO"` brings it back when a backtest needs debugging.
- `weekly` is how `enoshima run --weekly` replays a week: `preload_days`
  before its Monday, to warm the strategy up, and `overlap_days` after its
  Sunday, to close the trades the week opened. A trade is the week's if it
  opened in it. Both are part of a weekly run's id, so a different preload or
  overlap starts a different set of weeks. See `enoshima.weekly`.

This is the environment's file rather than a set of flags because it describes
the environment: how its runs are costed and sized. An unknown key is
refused rather than ignored, so a misspelt `weekly` cannot quietly run with the
defaults.

Three keys of earlier files are accepted and ignored, with a warning
(`RETIRED`). `influx` turned on recording to InfluxDB, which a run no longer
does -- everything it recorded is in the data lake (`enoshima.lake`).
`selection` and `walk_forward` configured choosing a parameter set from the
results, which Enoshima no longer does. Refusing them would stop every env
written before then from running at all.
"""

import json
import logging
from dataclasses import dataclass, field, fields
from logging import getLogger
from pathlib import Path

from qate.exchange import FeeSchedule

from . import fees
from .data_source import DEFAULT_CHUNK_DAYS
from .runner import MAX_CONCURRENCY
from .weekly import WeeklySettings

LOG = getLogger(__name__)

SETTINGS_FILE = "enoshima.json"
# Keys of a file written for an earlier Enoshima: accepted, ignored, and warned about.
RETIRED = {
    "influx": "a run records to the data lake, not InfluxDB; remove the entry",
    "selection": "Enoshima no longer chooses a parameter set from the results; remove the entry",
    "walk_forward": "Enoshima no longer chooses a parameter set from the results; remove the entry",
}


@dataclass
class Settings:
    chunk_days: int = DEFAULT_CHUNK_DAYS
    max_concurrency: int = MAX_CONCURRENCY
    strategy_log_level: str = "WARNING"
    fee_rates: dict[str, FeeSchedule] = field(default_factory=dict)
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
    for key, why in RETIRED.items():
        if key in data:
            data.pop(key)
            LOG.warning(f'"{key}" in {path} is ignored: {why}')
    _check_keys(data, Settings, str(path))

    fee_rates = fees.parse(data.pop("fee_rates", {}), str(path))

    sections = {}
    for name, cls in (("weekly", WeeklySettings),):
        section = data.pop(name, None)
        if section is None:
            section = cls()
        else:
            if not isinstance(section, dict):
                raise ValueError(f'"{name}" in {path} must be an object')
            _check_keys(section, cls, f'"{name}" in {path}')
            section = cls(**section)
        sections[name] = section
    return Settings(**data, fee_rates=fee_rates, **sections)
