"""How an environment is backtested: `enoshima.json`, beside the env's other files.

```json
{
  "influx": {"keys": "DEFAULT", "org": null},
  "chunk_days": 2,
  "max_concurrency": 16
}
```

Every key is optional, and a missing file means the defaults.

- `influx` turns on recording to InfluxDB. Absent or `null`, a run records
  nothing and still has its result. `keys` names the section of
  `~/.qate/influxdb.keys`; `org` defaults to the only one the token sees.
- `chunk_days` is how many days are loaded at once (memory).
- `max_concurrency` is how many parameter sets share one pass over the data.

This is the environment's file rather than a set of flags because it describes
the environment: where its runs are charted and how big its data is. An unknown
key is refused rather than ignored, so a misspelt `influx` cannot quietly leave a
run unrecorded.
"""

import json
from dataclasses import dataclass, fields
from pathlib import Path

from .data_source import DEFAULT_CHUNK_DAYS
from .runner import MAX_CONCURRENCY

SETTINGS_FILE = "enoshima.json"


@dataclass
class InfluxSettings:
    keys: str = "DEFAULT"
    org: str | None = None


@dataclass
class Settings:
    chunk_days: int = DEFAULT_CHUNK_DAYS
    max_concurrency: int = MAX_CONCURRENCY
    influx: InfluxSettings | None = None


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
            raise ValueError(f'"influx" in {path} must be an object, e.g. {{"keys": "DEFAULT"}}')
        _check_keys(influx, InfluxSettings, f'"influx" in {path}')
        influx = InfluxSettings(**influx)
    return Settings(**data, influx=influx)
