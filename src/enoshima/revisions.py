"""Moving recorded runs to the revision they are identified by now, and joining the halves of one.

A run's id holds its strategy revision (`strategy_env`). That revision was the
strategy repository's commit once; it is the variant's source now. Two
operations bring folders written under the old scheme, or split across two
revisions, together without replaying anything:

- `migrate`: a run whose revision is a clean commit is given the source
  revision of its variant at that commit, read from git -- the revision a run
  of the same source gets today -- and is moved to the id that gives. A
  weekly run whose new id is already taken is merged into it instead.
- `merge`: one weekly run's weeks are copied into another of the same
  environment, strategy, parameters, fees, preload and overlap, and its row is
  rebuilt from all of them (`weekly.rebuild`).

**A merge never joins two versions of a strategy unknowingly.** The two
revisions must name the same source: a source revision is itself, a clean
commit is the source at that commit, and a `+dirty` one names nothing that can
be read back. Unless both name the same source, `merge` refuses, and
`--unverified` is the caller saying they are the same anyway -- recorded in
every copied week's meta and in the run's `merged`, so the claim stays
attributable. A week both folders hold must be the same week, trade for trade,
or nothing is merged.

The merged-from run loses its `BacktestRun` row and its files are moved to
`run_id=<id>.merged`, which `lake.Store` does not list. Nothing is deleted:
Enoshima never deletes results.

Both take `apply=False` by default and only report what they would do.
"""

import re
from dataclasses import dataclass, field
from logging import getLogger

from . import weekly
from .lake import WHOLE, Store
from .run_log import identity_from_meta, run_id
from .strategy_env import SOURCE_REV_PREFIX, source_rev, sources_at_commit

LOG = getLogger(__name__)

MERGED_SUFFIX = ".merged"
_COMMIT = re.compile(r"[0-9a-f]{12}")
# The meta fields a week shares with its root; the rest are the week's own.
_WEEK_ONLY = ("start_ts", "batch_id", "start_str", "end_str", "week", "partial", "end_ts", "execution_time")


@dataclass
class Report:
    renamed: list[tuple[str, str]] = field(default_factory=list)
    merged: list[tuple[str, str, int]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _variant(meta: dict) -> tuple[str, str]:
    trading = meta.get("trading") or {}
    return trading["module_name"], trading["variant_name"]


def source_of(meta: dict) -> str | None:
    """The source revision a run's revision names, or None if it names none that can be read."""
    rev = meta.get("strategy_rev") or ""
    if rev.startswith(SOURCE_REV_PREFIX):
        return rev
    if _COMMIT.fullmatch(rev):
        try:
            return source_rev(sources_at_commit(*_variant(meta), rev))
        except Exception as e:  # not in this checkout's history, or the variant was not there
            LOG.warning(f"Cannot read {meta.get('strategy')} at {rev}: {e}")
    return None


def _id_holds(rid: str, meta: dict) -> bool:
    """Whether the meta hashes back to the run's id: if not, it cannot be re-identified safely."""
    try:
        return run_id(identity_from_meta(meta)) == rid
    except Exception:
        return False


def _weeks(store: Store, run_id: str) -> list[str]:
    return [p for p in store.parts(run_id) if p != WHOLE]


def _run_meta(store: Store, run_id: str) -> dict:
    """What a run's parts share: its row's meta, or a part's without the part's own fields."""
    if store.is_complete(run_id):
        meta = store.read_run(run_id)["meta"]
    else:
        meta = store.read_part(run_id, store.parts(run_id)[0])["meta"]
    return {k: v for k, v in meta.items() if k not in _WEEK_ONLY and k != "weeks"}


def merge(store: Store, source_id: str, target_id: str, *, unverified: bool = False, apply: bool = False) -> int:
    """Copy `source_id`'s weeks into `target_id`, rebuild it, and set the source aside. Returns weeks copied."""
    src_weeks, dst_weeks = set(_weeks(store, source_id)), set(_weeks(store, target_id))
    if not src_weeks or not dst_weeks:
        raise ValueError(f"Both runs must be weekly runs with weeks in the lake: {source_id}, {target_id}")
    src_meta, dst_meta = _run_meta(store, source_id), _run_meta(store, target_id)

    same = identity_from_meta(src_meta, "x") == identity_from_meta(dst_meta, "x")
    if not same:
        raise ValueError(f"{source_id} and {target_id} differ in more than their revision: they are not one run")
    src_source, dst_source = source_of(src_meta), source_of(dst_meta)
    verified = src_source is not None and src_source == dst_source
    if not verified and not unverified:
        raise ValueError(
            f"Cannot show {src_meta.get('strategy_rev')} and {dst_meta.get('strategy_rev')} run the same source "
            f"({src_source or 'unreadable'} vs {dst_source or 'unreadable'}). If you know they do, merge with --unverified."
        )
    for key in sorted(src_weeks & dst_weeks):
        if store.read_part_trades(source_id, key) != store.read_part_trades(target_id, key):
            raise ValueError(
                f"Week {key} differs between {source_id} and {target_id}: they did not compute the same thing"
            )

    todo = sorted(src_weeks - dst_weeks)
    LOG.info(
        f"merge {source_id} -> {target_id}: {len(todo)} week(s) to copy, "
        f"{len(src_weeks & dst_weeks)} shared and identical, "
        f"{'verified' if verified else 'UNVERIFIED'} ({src_meta.get('strategy_rev')} -> {dst_meta.get('strategy_rev')})"
    )
    if not apply:
        return len(todo)

    provenance = {"run_id": source_id, "strategy_rev": src_meta.get("strategy_rev"), "verified": verified}
    for key in todo:
        meta = store.read_part(source_id, key)["meta"]
        meta.update(id=target_id, strategy_rev=dst_meta.get("strategy_rev"), merged_from=provenance)
        store.copy_part(source_id, key, target_id, meta)
    dst_meta["merged"] = [*dst_meta.get("merged", []), {**provenance, "weeks": todo}]
    weekly.rebuild(store, target_id, dst_meta)
    store.remove_run(source_id)
    store.rename_run(source_id, source_id + MERGED_SUFFIX)
    return len(todo)


def migrate(store: Store, *, apply: bool = False) -> Report:
    """Give every run recorded at a clean commit the source revision of its variant at that commit."""
    report = Report()
    for rid in store.all_run_ids():
        meta = store.read_run(rid)["meta"] if store.is_complete(rid) else _run_meta(store, rid)
        rev = meta.get("strategy_rev") or ""
        if rev.startswith(SOURCE_REV_PREFIX):
            continue
        if not _COMMIT.fullmatch(rev):
            report.skipped.append((rid, f"revision {rev or 'none'} names no source that can be read"))
            continue
        if not _id_holds(rid, meta):
            report.skipped.append((rid, "its meta does not hash back to its id"))
            continue
        new_rev = source_of(meta)
        if new_rev is None:
            report.skipped.append((rid, f"cannot read {meta.get('strategy')} at {rev}"))
            continue
        new_id = run_id(identity_from_meta(meta, new_rev))
        if new_id in store.all_run_ids():
            if meta.get("mode") != "weekly":
                report.skipped.append((rid, f"{new_id} already exists"))
                continue
            _rekey(store, rid, meta, new_rev, new_id, apply=apply, rename=False)
            report.merged.append((rid, new_id, merge(store, rid, new_id, apply=apply)))
            continue
        _rekey(store, rid, meta, new_rev, new_id, apply=apply, rename=True)
        report.renamed.append((rid, new_id))
    LOG.info(
        f"migrate-revisions: {len(report.renamed)} renamed, {len(report.merged)} merged, "
        f"{len(report.skipped)} left as they are{'' if apply else ' (dry run)'}"
    )
    for name, why in report.skipped:
        LOG.info(f"  left {name}: {why}")
    return report


def _rekey(store: Store, rid: str, meta: dict, new_rev: str, new_id: str, *, apply: bool, rename: bool) -> None:
    """Record the source revision in a run and its parts; move it to the id that revision gives."""
    LOG.info(f"  {rid} -> {new_id}  ({meta.get('strategy_rev')} = {new_rev}){'' if apply else '  [dry run]'}")
    if not apply:
        return
    commit = meta.get("strategy_rev")

    def updated(m: dict) -> dict:
        m.update(strategy_rev=new_rev, strategy_commit=m.get("strategy_commit") or commit)
        if rename:
            m["id"] = new_id
        return m

    for part in store.parts(rid):
        store.write_part_meta(rid, part, updated(store.read_part(rid, part)["meta"]))
    if store.is_complete(rid):
        run = store.read_run(rid)
        store.write_run(rid, updated(run["meta"]), run["summary"], store.trades(rid))
    if rename:
        store.rename_run(rid, new_id)
