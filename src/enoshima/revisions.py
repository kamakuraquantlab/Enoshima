"""Moving recorded runs to the revision they are identified by now, and joining the halves of one.

A run's id holds its strategy revision (`strategy_env`). That revision was the
strategy repository's commit once; it is the variant's source now. Two
operations bring folders written under the old scheme, or split across two
revisions, together without replaying anything:

- `migrate`: a folder whose revision is a clean commit is given the source
  revision of its variant at that commit, read from git -- the revision a run
  of the same source gets today -- and is renamed to the id that gives. A
  weekly folder whose new id is already taken is merged into it instead.
- `merge`: one weekly folder's weeks are copied into another of the same
  environment, strategy, parameters, fees, preload and overlap, and the root is
  rebuilt from all of them (`weekly.rebuild`).

**A merge never joins two versions of a strategy unknowingly.** The two
revisions must name the same source: a source revision is itself, a clean
commit is the source at that commit, and a `+dirty` one names nothing that can
be read back. Unless both name the same source, `merge` refuses, and
`--unverified` is the caller saying they are the same anyway -- recorded in
every copied week's `meta.json` and in the root's `merged`, so the claim stays
attributable. A week both folders hold must be the same week, trade for trade,
or nothing is merged.

The merged-from folder is renamed `<id>.merged`, which `run_dir.iter_complete`
does not list. Nothing is deleted: Enoshima never deletes results.

Both take `apply=False` by default and only report what they would do.
"""

import re
import shutil
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path

from . import run_dir, weekly
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


def _meta(path: Path) -> dict:
    return run_dir.read_json(path, run_dir.META)


def _write_meta(path: Path, meta: dict) -> None:
    run_dir._dump(path / run_dir.META, meta)


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


def _id_holds(path: Path, meta: dict) -> bool:
    """Whether the meta hashes back to the folder's name: if not, it cannot be re-identified safely."""
    try:
        return run_id(identity_from_meta(meta)) == path.name
    except Exception:
        return False


def _week_dirs(path: Path) -> dict[str, Path]:
    weeks = path / run_dir.WEEKS_DIR
    return {d.name: d for d in sorted(weeks.iterdir()) if run_dir.is_complete(d)} if weeks.is_dir() else {}


def _root_meta(path: Path, weeks: dict[str, Path]) -> dict:
    """What a weekly run's weeks share: its root meta, or a week's without the week's own fields."""
    if (path / run_dir.META).is_file():
        meta = _meta(path)
    else:
        meta = _meta(next(iter(weeks.values())))
    return {k: v for k, v in meta.items() if k not in _WEEK_ONLY and k != "weeks"}


def merge(root: Path, source_id: str, target_id: str, *, unverified: bool = False, apply: bool = False) -> int:
    """Copy `source_id`'s weeks into `target_id`, rebuild it, and set the source aside. Returns weeks copied."""
    root = Path(root)
    source, target = run_dir.run_dir(root, source_id), run_dir.run_dir(root, target_id)
    src_weeks, dst_weeks = _week_dirs(source), _week_dirs(target)
    if not src_weeks or not dst_weeks:
        raise ValueError(f"Both runs must be weekly runs with weeks on disk: {source_id}, {target_id}")
    src_meta, dst_meta = _root_meta(source, src_weeks), _root_meta(target, dst_weeks)

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
    for key in sorted(src_weeks.keys() & dst_weeks.keys()):
        a = (src_weeks[key] / run_dir.TRADES).read_bytes()
        b = (dst_weeks[key] / run_dir.TRADES).read_bytes()
        if a != b:
            raise ValueError(
                f"Week {key} differs between {source_id} and {target_id}: they did not compute the same thing"
            )

    todo = sorted(src_weeks.keys() - dst_weeks.keys())
    LOG.info(
        f"merge {source_id} -> {target_id}: {len(todo)} week(s) to copy, "
        f"{len(src_weeks.keys() & dst_weeks.keys())} shared and identical, "
        f"{'verified' if verified else 'UNVERIFIED'} ({src_meta.get('strategy_rev')} -> {dst_meta.get('strategy_rev')})"
    )
    if not apply:
        return len(todo)

    provenance = {"run_id": source_id, "strategy_rev": src_meta.get("strategy_rev"), "verified": verified}
    for key in todo:
        copied = run_dir.week_dir(root, target_id, key)
        shutil.copytree(src_weeks[key], copied)
        meta = _meta(copied)
        meta.update(id=target_id, strategy_rev=dst_meta.get("strategy_rev"), merged_from=provenance)
        _write_meta(copied, meta)
    dst_meta["merged"] = [*dst_meta.get("merged", []), {**provenance, "weeks": todo}]
    weekly.rebuild(root, target_id, dst_meta)
    source.rename(source.with_name(source.name + MERGED_SUFFIX))
    return len(todo)


def migrate(root: Path, *, apply: bool = False) -> Report:
    """Give every folder recorded at a clean commit the source revision of its variant at that commit."""
    root = Path(root)
    report = Report()
    base = root / run_dir.RUN_LOG_DIR
    for path in sorted(base.iterdir()) if base.is_dir() else []:
        if not path.is_dir() or "." in path.name or not (path / run_dir.META).is_file():
            continue
        meta = _meta(path)
        rev = meta.get("strategy_rev") or ""
        if rev.startswith(SOURCE_REV_PREFIX):
            continue
        if not _COMMIT.fullmatch(rev):
            report.skipped.append((path.name, f"revision {rev or 'none'} names no source that can be read"))
            continue
        if not _id_holds(path, meta):
            report.skipped.append((path.name, "its meta.json does not hash back to its id"))
            continue
        new_rev = source_of(meta)
        if new_rev is None:
            report.skipped.append((path.name, f"cannot read {meta.get('strategy')} at {rev}"))
            continue
        new_id = run_id(identity_from_meta(meta, new_rev))
        target = path.with_name(new_id)
        if target.exists():
            if meta.get("mode") != "weekly":
                report.skipped.append((path.name, f"{new_id} already exists"))
                continue
            _rekey(path, meta, new_rev, new_id, apply=apply, rename=False)
            report.merged.append((path.name, new_id, merge(root, path.name, new_id, apply=apply)))
            continue
        _rekey(path, meta, new_rev, new_id, apply=apply, rename=True)
        report.renamed.append((path.name, new_id))
    LOG.info(
        f"migrate-revisions: {len(report.renamed)} renamed, {len(report.merged)} merged, "
        f"{len(report.skipped)} left as they are{'' if apply else ' (dry run)'}"
    )
    for name, why in report.skipped:
        LOG.info(f"  left {name}: {why}")
    return report


def _rekey(path: Path, meta: dict, new_rev: str, new_id: str, *, apply: bool, rename: bool) -> None:
    """Record the source revision in a folder and its weeks; rename it to the id that revision gives."""
    LOG.info(f"  {path.name} -> {new_id}  ({meta.get('strategy_rev')} = {new_rev}){'' if apply else '  [dry run]'}")
    if not apply:
        return
    commit = meta.get("strategy_rev")
    for meta_path in [path, *_week_dirs(path).values()]:
        m = _meta(meta_path)
        m.update(strategy_rev=new_rev, strategy_commit=m.get("strategy_commit") or commit)
        if rename:
            m["id"] = new_id
        _write_meta(meta_path, m)
    if rename:
        path.rename(path.with_name(new_id))
