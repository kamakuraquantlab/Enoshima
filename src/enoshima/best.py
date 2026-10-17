"""An environment's recommendations: one per range and per group of parameter sets.

`recommend` reads every finished run folder under `run_log/`, and

1. groups them by strategy and range -- two runs over different days, or of
   different variants, are not measuring the same thing;
2. splits each of those by the values of `group_by`, the categorical
   parameters by default;
3. applies `selection` to each group on its own, and recommends from it.

A categorical parameter -- a trading schedule, a mode -- is a different way of
trading, not a point on a scale: it has no neighbours, and choosing one across
all of them answers a different question -- which mode -- with the same noisy
figures that choose a box size. Grouped, each mode gets its best set, and the
modes can be compared on their own merits.

Every range is recommended for, not only the one a sweep just ran, so a
recommendation follows a change to the rule without a replay. Neighbours come
from the values the group's runs actually took, so an older range is judged by
the grid it was run on.

The result goes to `best_result/<batch_id>/`: `selection.json` with every
group, and a copy of each recommended run's `meta.json` as `<run_id>.json`.
"""

import json
import shutil
from logging import getLogger
from pathlib import Path

from qate.util.dt_range import DtRange

from . import run_dir, selection
from .ledger import GROUPINGS

LOG = getLogger(__name__)

BEST_RESULT_DIR = "best_result"
SELECTION_FILE = "selection.json"


def load_runs(root: Path, grouping: str = "month") -> list[dict]:
    """Every finished, non-temporary run folder with a pnl5 figure, as `selection` reads it.

    `periods` and `trade_pnl5` are keyed by `grouping`, a calendar one of
    `ledger.GROUPINGS`: month for `selection`, day for `post_analysis`.
    """
    key_of = GROUPINGS[grouping][0]
    runs = []
    for path in run_dir.iter_complete(root):
        try:
            meta = run_dir.read_json(path, run_dir.META)
            summary = run_dir.read_json(path, run_dir.SUMMARY)
            if "pnl5" not in summary or not meta.get("start_str") or not meta.get("end_str"):
                continue
            if meta.get("is_temporary_run"):
                continue  # a one-off look at a set, not a candidate
            periods = run_dir.read_json(path, run_dir.grouping_file(grouping))
            trade_pnl5 = {}
            for t in run_dir.read_trades(path):
                trade_pnl5.setdefault(key_of(t), []).append(t.pnl_change5)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            LOG.error(f"Could not read {path}: {e}")
            continue
        runs.append({**meta, "run_id": path.name,
                     "result": {**summary, "periods": periods, "trade_pnl5": trade_pnl5}})
    return runs


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def categorical(runs: list[dict]) -> list[str]:
    """The parameters some run gave a value that is not a number, in the order first seen."""
    names = []
    for r in runs:
        for name, v in (r.get("params") or {}).items():
            if name not in names and not _is_number(v):
                names.append(name)
    return names


def axes(runs: list[dict]) -> list[dict]:
    """Each parameter's values among runs, shaped like the strategy grid for neighbour selection."""
    values: dict[str, list] = {}
    for r in runs:
        for name, v in (r.get("params") or {}).items():
            if v not in values.setdefault(name, []):
                values[name].append(v)
    return [{"name": name, "values": vs} for name, vs in values.items()]


def group(runs: list[dict], group_by: list[str] | None) -> dict[tuple, dict[tuple, list[dict]]]:
    """`(strategy, start_str, end_str)` -> `((name, value), ...)` -> the runs."""
    out: dict[tuple, dict[tuple, list[dict]]] = {}
    by_range: dict[tuple, list[dict]] = {}
    for r in runs:
        by_range.setdefault((r.get("strategy"), r["start_str"], r["end_str"]), []).append(r)
    for key, members in by_range.items():
        names = categorical(members) if group_by is None else list(group_by)
        groups = out[key] = {}
        for r in members:
            params = r.get("params") or {}
            groups.setdefault(tuple((n, params.get(n)) for n in names), []).append(r)
    return out


def recommend(root: Path, batch_id: int, settings: selection.SelectionSettings) -> list[dict]:
    """Choose for every range and group under `root`, write `best_result/<batch_id>/`, and return the groups."""
    runs = load_runs(root)
    out = []
    for (strategy, start_str, end_str), groups in sorted(group(runs, settings.group_by).items(),
                                                         key=lambda kv: tuple(str(k) for k in kv[0])):
        dt_range = DtRange.from_strings(start_str, end_str)
        for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
            choice = selection.select(members, dt_range.start_ts, dt_range.end_ts, axes(members), settings)
            top = choice["ranking"][0]["params"] if choice["ranking"] else None
            chosen = next((r for r in members if r["params"] == top), None) if choice["recommended"] else None
            out.append({
                "strategy": strategy,
                "start_str": start_str,
                "end_str": end_str,
                "group": dict(key),
                "runs": len(members),
                "recommended_run_id": chosen["run_id"] if chosen else None,
                **choice,
            })

    best_dir = Path(root) / BEST_RESULT_DIR / str(batch_id)
    best_dir.mkdir(parents=True, exist_ok=True)
    (best_dir / SELECTION_FILE).write_text(json.dumps({"settings": out[0]["settings"] if out else None,
                                                       "groups": out}, indent=2))
    for g in out:
        if g["recommended_run_id"]:
            shutil.copyfile(run_dir.run_dir(root, g["recommended_run_id"]) / run_dir.META,
                            best_dir / f"{g['recommended_run_id']}.json")
    for g in out:
        _log(g)
    LOG.info(f"Saved {best_dir}: {sum(1 for g in out if g['recommended_run_id'])} recommended "
             f"in {len(out)} group(s)")
    return out


def _fmt(value, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def _show(params) -> str:
    return " ".join(f"{k}={v}" for k, v in (params or {}).items()) or "none"


def _log(g: dict) -> None:
    where = (f"{g['strategy']} {g['start_str']}..{g['end_str']} "
             f"{_show(g['group']) if g['group'] else 'all'} ({g['runs']} runs)")
    if g["method"] == "walk-forward":
        oos = g["walk_forward"]["out_of_sample"]
        how = (f"walk-forward {oos['folds']} folds, out of sample pnl5={_fmt(oos['pnl5'], '+.0f')} "
               f"pnl10={_fmt(oos['pnl10'], '+.0f')} profitable {oos['profitable_folds']}/{oos['folds']}")
    else:
        how = f"whole range ({g['reason']})"
    top = g["ranking"][0] if g["ranking"] else None
    if not g["recommended_run_id"]:
        LOG.warning(f"BEST {where}: nothing passes the filters; {how}")
        return
    LOG.info(
        f"BEST {where}: {g['recommended_run_id']} score={_fmt(top['score'], '.3f')} "
        f"pnl5={_fmt(top['pnl5'], '+.0f')} pnl10={_fmt(top['pnl10'], '+.0f')} dd5={_fmt(top['dd5'], '.0f')} "
        f"params=({_show(top['params'])}); {how}"
    )
