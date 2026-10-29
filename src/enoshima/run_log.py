"""What a run recorded about itself: its `meta`, in the lake's `BacktestRun` and `BacktestPart`.

A run log is the provenance of a number. It holds the environment, the library
revision, the config, the exact parameters and the date range, so a figure in a
notebook or an article can be traced back to the run that produced it and
re-run. The numbers themselves are beside it, under the same run id in the
lake; see `lake`.

## The id is deterministic, and that is what makes a run resumable

The id is a hash of everything that decides what a run computes: the
environment, the strategy and variant, the revision of the variant's source
(`strategy_env.strategy_rev`), the parameter set, the non-zero fee rates of
the markets it trades (`fees.identity`), and the date range.
Two runs that would compute the same thing therefore have the same id -- the
`run_id` partition of their files -- so `Runner` can see that a result already exists and
skip it. An optimize sweep over
two hundred parameter sets that dies at number ninety resumes at ninety rather
than starting again.

It was a random UUID before, which meant the "a run log exists for this range"
check could never once have fired — every fresh log had a new id and every
combination was recomputed. `--force` is now the way to say recompute anyway.

A weekly run's id (`weekly`) has no dates in it, and the preload and overlap instead: it
holds every week computed for that environment, strategy and parameter
set, and a window is whichever of them it covers.

Any other run is temporary (`is_temporary_run`): a quick look at some days,
kept and listed by `results`, and never a candidate for `select` or
`walk-forward`, which compare weekly runs.
"""

import hashlib
import json
import time

from qate.exchange import FeeSchedule
from qate.util.dt_range import DtRange
from qate.util.encoder import Encoder
from qate_env.env import Env
from qate_env.run_log import RunLog

from . import fees
from .weekly import Week, WeeklySettings


def identity(env: str, strategy: str, strategy_rev: str, params: dict, fee_rates: dict, dt_range, weekly) -> dict:
    """What decides a run's numbers, and nothing else: hashed, it is the run's id.

    `dt_range` is `(start_str, end_str)` and counts only for a run that is not
    weekly; `weekly` is the preload and overlap of one that is.
    """
    data = {"env": env, "strategy": strategy, "strategy_rev": strategy_rev, "params": params}
    # Only when one is non-zero: a zero rate computes what a run made before
    # rates were stated, and must keep that run's id.
    if charged := fees.identity(fee_rates):
        data["fee_rates"] = charged
    if weekly is None:
        data.update({"start": dt_range[0], "end": dt_range[1]})
    else:
        data["weekly"] = weekly
    return data


def run_id(identity: dict) -> str:
    raw = json.dumps(identity, sort_keys=True, cls=Encoder)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def identity_from_meta(meta: dict, strategy_rev: str | None = None) -> dict:
    """A recorded run's identity, from its meta, with its revision replaced if given.

    With the meta's own revision it should hash back to the run's id; a
    caller relying on this checks that before trusting it.
    """
    weekly = None
    if meta.get("mode") == "weekly":
        weekly = {"preload_days": meta["preload_days"], "overlap_days": meta["overlap_days"]}
    return identity(
        meta["env"],
        meta["strategy"],
        strategy_rev or meta.get("strategy_rev"),
        meta["params"],
        fees.parse(meta.get("fee_rates") or {}, f"run {meta.get('id')}"),
        (meta.get("start_str"), meta.get("end_str")),
        weekly,
    )


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
        strategy_commit: str | None = None,
        fee_rates: dict[str, FeeSchedule] | None = None,
    ):
        # config and profile go to RunLog, which records them: they are the
        # provenance of the result, and a run log without them cannot say which
        # market or order size produced a number.
        super().__init__(env, config=config, profile=profile, params=params)
        self.dt_range = dt_range
        self.strategy_name = strategy_name
        self.strategy_rev = strategy_rev
        self.strategy_commit = strategy_commit
        self.fee_rates = fee_rates or {}
        self.batch_id = batch_id
        self.param_set_id = None
        self.result = None
        self.end_ts = None
        # Set for one week of a weekly run: the preload and overlap are in the id, the week is not.
        self.weekly = weekly
        self.week_key = week_key

    def get_id(self) -> str:
        """A hash of what the run computes, so an identical run is recognisable.

        Deliberately excludes the batch id and the timestamps: those differ between two runs that would produce the same
        numbers, and the point is to notice that they would. A `--force` re-run
        replaces the run's files in the lake.
        """
        weekly = None
        if self.week_key is not None:
            weekly = {"preload_days": self.weekly.preload_days, "overlap_days": self.weekly.overlap_days}
        return run_id(identity(str(self.env.name), self.strategy_name, self.strategy_rev, self.params,
                               self.fee_rates, (self.dt_range.start_str, self.dt_range.end_str), weekly))

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(
            {
                "batch_id": self.batch_id,
                "param_set_id": self.param_set_id,
                "params": self.params,
                "strategy": self.strategy_name,
                "strategy_rev": self.strategy_rev,
                "strategy_commit": self.strategy_commit,
                "fee_rates": fees.to_json(self.fee_rates),
                "start_str": self.dt_range.start_str,
                "end_str": self.dt_range.end_str,
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
        """What every week of a weekly run shares, for its `BacktestRun` row: no range, no batch, no timings."""
        data = self.to_dict()
        for key in ("start_ts", "batch_id", "start_str", "end_str", "week", "partial", "end_ts", "execution_time"):
            data.pop(key, None)
        return data

    def set_result(self, result: dict) -> None:
        """The run's summary, which `lake` writes beside this log rather than in it."""
        self.result = result
        self.end_ts = time.time()
