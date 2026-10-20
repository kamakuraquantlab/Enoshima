"""What a run recorded about itself: its folder's `meta.json`.

A run log is the provenance of a number. It holds the environment, the library
revision, the config, the exact parameters and the date range, so a figure in a
notebook or an article can be traced back to the run that produced it and
re-run. The numbers themselves are beside it in the run's folder; see
`run_dir`.

## The id is deterministic, and that is what makes a run resumable

The id is a hash of everything that decides what a run computes: the
environment, the strategy and variant, the revision of the strategy's code
(`strategy_env.strategy_rev`), the parameter set, and the date range.
Two runs that would compute the same thing therefore have the same id -- the
name of their folder -- so `Runner` can see that a result already exists and
skip it. An optimize sweep over
two hundred parameter sets that dies at number ninety resumes at ninety rather
than starting again.

It was a random UUID before, which meant the "a run log exists for this range"
check could never once have fired — every fresh log had a new id and every
combination was recomputed. `--force` is now the way to say recompute anyway.

A weekly run's id (`weekly`) has no dates in it, and the preload and overlap instead: its
folder holds every week computed for that environment, strategy and parameter
set, and a window is whichever of them it covers.

Any other run is temporary (`is_temporary_run`): a quick look at some days,
kept and listed by `results`, and never a candidate for `select` or
`walk-forward`, which compare weekly runs.
"""

import hashlib
import json
import time

from qate.util.dt_range import DtRange
from qate.util.encoder import Encoder
from qate_env.env import Env
from qate_env.run_log import RunLog

from .weekly import Week, WeeklySettings


class BacktestLog(RunLog):
    def __init__(
        self,
        env: Env,
        params: dict,
        dt_range: DtRange,
        strategy_name: str,
        batch_id: int = None,
        config=None,
        profile=None,
        weekly: WeeklySettings | None = None,
        week_key: str | None = None,
        strategy_rev: str | None = None,
    ):
        # config and profile go to RunLog, which records them: they are the
        # provenance of the result, and a run log without them cannot say which
        # market or order size produced a number.
        super().__init__(env, config=config, profile=profile, params=params)
        self.dt_range = dt_range
        self.strategy_name = strategy_name
        self.strategy_rev = strategy_rev
        self.batch_id = batch_id
        self.param_set_id = None
        self.influx_bucket = None
        self.result = None
        self.end_ts = None
        # Set for one week of a weekly run: the preload and overlap are in the id, the week is not.
        self.weekly = weekly
        self.week_key = week_key

    def get_id(self) -> str:
        """A hash of what the run computes, so an identical run is recognisable.

        Deliberately excludes the batch id and the timestamps: those differ between two runs that would produce the same
        numbers, and the point is to notice that they would. The batch id is in the
        InfluxDB bucket instead, so a `--force` re-run replaces this folder and writes
        a bucket of its own.
        """
        identity = {
            "env": str(self.env.name),
            "strategy": self.strategy_name,
            "strategy_rev": self.strategy_rev,
            "params": self.params,
        }
        if self.week_key is None:
            identity.update({"start": self.dt_range.start_str, "end": self.dt_range.end_str})
        else:
            identity["weekly"] = {"preload_days": self.weekly.preload_days, "overlap_days": self.weekly.overlap_days}
        raw = json.dumps(identity, sort_keys=True, cls=Encoder)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(
            {
                "batch_id": self.batch_id,
                "param_set_id": self.param_set_id,
                "params": self.params,
                "strategy": self.strategy_name,
                "strategy_rev": self.strategy_rev,
                "start_str": self.dt_range.start_str,
                "end_str": self.dt_range.end_str,
                "influx_bucket": self.influx_bucket,
            }
        )
        if self.week_key is not None:
            whole = Week.parse(self.week_key).dt_range
            data.update({
                "mode": "weekly",
                "week": self.week_key,
                "preload_days": self.weekly.preload_days,
                "overlap_days": self.weekly.overlap_days,
                "partial": (self.dt_range.start_str, self.dt_range.end_str) != (whole.start_str, whole.end_str),
            })
        # Only a weekly run is a candidate for `select` and `walk-forward`; any other is a quick look.
        data["is_temporary_run"] = self.week_key is None
        data["id"] = self.get_id()
        if self.end_ts is not None:
            data["end_ts"] = self.end_ts
            data["execution_time"] = str(int(self.end_ts - self.start_ts)) + "s"
        return data

    def weekly_meta(self) -> dict:
        """What every week of a weekly run shares, for its root `meta.json`: no range, no batch, no timings."""
        data = self.to_dict()
        for key in ("start_ts", "batch_id", "start_str", "end_str", "week", "partial", "end_ts", "execution_time"):
            data.pop(key, None)
        return data

    def set_result(self, result: dict) -> None:
        """The run's summary, which `run_dir` writes beside this log rather than in it."""
        self.result = result
        self.end_ts = time.time()
