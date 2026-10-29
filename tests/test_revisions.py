"""Runs recorded at a commit take their source revision; two halves of one run become one."""

import subprocess
from pathlib import Path

import pytest
import test_weekly
from test_run_end_to_end import RunRef
from test_weekly import W02, W03, run, use, weeks_range

import enoshima.runner
from enoshima import revisions, weekly
from enoshima.strategy_env import strategy_rev

REPO = Path(__file__).resolve().parent.parent
make_env = test_weekly.make_env  # the fixture, shared


def folders(env) -> list[RunRef]:
    """Every run of the env, finished or not, but for one set aside by a merge."""
    store = use(env)
    return [RunRef(store, r) for r in store.all_run_ids()]


def weeks_of(ref: RunRef) -> list[str]:
    return [w.key for w in weekly.on_disk(ref.store, ref.name)]


def meta_of(ref: RunRef, week: str | None = None) -> dict:
    return ref.store.read_run(ref.name)["meta"] if week is None else ref.store.read_part(ref.name, week)["meta"]


def two_halves(env, monkeypatch, first="aaaaaaaaaaaa+dirty.11111111", second="aaaaaaaaaaaa+dirty.22222222"):
    """W02-W03 under one revision, W03 again under another: a run split by a strategy change."""
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: first)
    run(env, weeks_range(W02, W03))
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: second)
    run(env, weeks_range(W03, W03))
    meta = {meta_of(p)["strategy_rev"]: p.name for p in folders(env)}
    return meta[first], meta[second]


def test_a_merge_needs_the_two_revisions_shown_to_be_one_source(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    with pytest.raises(ValueError, match="--unverified"):
        revisions.merge(use(env), older, newer, apply=True)
    assert len(folders(env)) == 2  # nothing moved


def test_an_unverified_merge_joins_the_halves_and_says_so(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    assert revisions.merge(use(env), older, newer, unverified=True) == 1  # a dry run counts
    assert len(folders(env)) == 2

    assert revisions.merge(use(env), older, newer, unverified=True, apply=True) == 1
    store = use(env)
    target = RunRef(store, newer)
    assert weeks_of(target) == ["2026-W02", "2026-W03"]
    assert store.is_complete(newer)  # the run's row is rebuilt over both weeks
    copied = meta_of(target, "2026-W02")
    assert copied["merged_from"] == {"run_id": older, "strategy_rev": "aaaaaaaaaaaa+dirty.11111111", "verified": False}
    assert meta_of(target)["merged"][0]["weeks"] == ["2026-W02"]
    # the source is set aside, not deleted, and no longer listed
    assert store.parts(f"{older}{revisions.MERGED_SUFFIX}") == ["2026-W02", "2026-W03"]
    assert store.run_ids() == [newer] and store.all_run_ids() == [newer]


def test_a_merge_refuses_a_shared_week_that_differs(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    store = use(env)
    part = store.read_part(older, "2026-W03")
    trades = store.read_part_trades(older, "2026-W03")
    store.write_part(older, "2026-W03", part["meta"], part["summary"], trades + trades[-1:])
    with pytest.raises(ValueError, match="2026-W03 differs"):
        revisions.merge(use(env), older, newer, unverified=True, apply=True)


def test_a_merge_of_one_source_under_two_names_needs_no_flag(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    monkeypatch.setattr(revisions, "source_of", lambda meta: "src.000000000000")
    revisions.merge(use(env), older, newer, apply=True)
    copied = meta_of(RunRef(use(env), newer), "2026-W02")
    assert copied["merged_from"]["verified"] is True


def _clean_commit() -> str:
    """HEAD of this checkout, if the example strategy is unchanged in it; else the test skips."""
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip()[:12]
        dirty = subprocess.check_output(["git", "status", "--porcelain", "example"], cwd=REPO).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("not a git checkout")
    if dirty:
        pytest.skip("example/ has uncommitted changes, so HEAD is not the source on disk")
    return head


def test_a_run_recorded_at_a_commit_moves_to_the_source_revision_and_is_reused(make_env, monkeypatch):
    commit = _clean_commit()
    env = make_env("env")
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: commit)
    run(env, weeks_range(W02, W02))
    [old] = folders(env)
    recorded = meta_of(old)["strategy_commit"]

    report = revisions.migrate(use(env), apply=True)
    [new] = folders(env)
    assert report.renamed == [(old.name, new.name)]
    meta = meta_of(new)
    assert meta["strategy_rev"] == strategy_rev("example.strategy", "v1")
    assert meta["strategy_commit"] == recorded and meta["id"] == new.name  # provenance kept as it was

    # A run of the same source today lands in that run and replays nothing it holds.
    monkeypatch.setattr(enoshima.runner, "strategy_rev", strategy_rev)
    runner = run(env, weeks_range(W02, W03))
    assert [p.name for p in folders(env)] == [new.name]
    assert weeks_of(new) == ["2026-W02", "2026-W03"]
    assert runner is not None


def test_a_dirty_revision_is_left_as_it_is(make_env, monkeypatch):
    env = make_env("env")
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: "aaaaaaaaaaaa+dirty.11111111")
    run(env, weeks_range(W02, W02))
    report = revisions.migrate(use(env), apply=True)
    assert not report.renamed and len(report.skipped) == 1
