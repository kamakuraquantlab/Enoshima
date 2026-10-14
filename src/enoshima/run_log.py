"""What a run recorded about itself, as one JSON file.

A run log is the provenance of a number. It holds the environment, the library
revision, the config, the exact parameters, the date range, and the evaluation
results, so a figure in a notebook or an article can be traced back to the run
that produced it and re-run.

## The id is deterministic, and that is what makes a run resumable

The id is a hash of everything that decides what a run computes: the
environment, the strategy and variant, the parameter set, and the date range.
Two runs that would compute the same thing therefore have the same id, so
`Runner` can see that a result already exists and skip it. An optimize sweep over
two hundred parameter sets that dies at number ninety resumes at ninety rather
than starting again.

It was a random UUID before, which meant the "a run log exists for this range"
check could never once have fired — every fresh log had a new id and every
combination was recomputed. `--force` is now the way to say recompute anyway.
"""

import hashlib
import json
import time

from qate.util.dt_range import DtRange
from qate.util.encoder import Encoder
from qate_env.env import Env
from qate_env.run_log import RunLog


class BacktestLog(RunLog):
    def __init__(
        self,
        env: Env,
        params: dict,
        dt_range: DtRange,
        strategy_name: str,
        is_temporary_run: bool = False,
        batch_id: int = None,
        config=None,
        profile=None,
    ):
        # config and profile go to RunLog, which records them: they are the
        # provenance of the result, and a run log without them cannot say which
        # market or order size produced a number.
        super().__init__(env, config=config, profile=profile, params=params)
        self.dt_range = dt_range
        self.strategy_name = strategy_name
        self.is_temporary_run = is_temporary_run
        self.batch_id = batch_id
        self.param_set_id = None
        self.influx_bucket = None
        self.result = None
        self.end_ts = None

    def get_id(self) -> str:
        """A hash of what the run computes, so an identical run is recognisable.

        Deliberately excludes the batch id and the timestamps: those differ between two runs that would produce the same
        numbers, and the point is to notice that they would.

        A temporary run is marked in it, so that it does not overwrite the log of
        the permanent run it repeats -- the log that names the bucket the results
        live in. Only when temporary, so a permanent run's id is what it always was.
        """
        identity = {
            "env": str(self.env.name),
            "strategy": self.strategy_name,
            "params": self.params,
            "start": self.dt_range.start_str,
            "end": self.dt_range.end_str,
        }
        if self.is_temporary_run:
            identity["temporary"] = True
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
                "start_str": self.dt_range.start_str,
                "end_str": self.dt_range.end_str,
                "is_temporary_run": self.is_temporary_run,
                "influx_bucket": self.influx_bucket,
            }
        )
        if self.result:
            data["result"] = self.result
            data["end_ts"] = self.end_ts
            data["execution_time"] = str(int(self.end_ts - self.start_ts)) + "s"
        return data

    def set_result(self, result: dict) -> None:
        self.result = result
        self.end_ts = time.time()
