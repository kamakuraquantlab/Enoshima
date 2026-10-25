"""Folders recorded at a commit take their source revision; two halves of one run become one."""

import subprocess
from pathlib import Path

import pytest
import test_weekly
from test_weekly import W02, W03, run, weeks_range

import enoshima.runner
from enoshima import revisions, run_dir, weekly
from enoshima.strategy_env import strategy_rev

REPO = Path(__file__).resolve().parent.parent
make_env = test_weekly.make_env  # the fixture, shared


def folders(env) -> list[Path]:
    return sorted(p for p in (Path(env.work_dir) / run_dir.RUN_LOG_DIR).iterdir())


def weeks_of(path: Path) -> list[str]:
    return [w.key for w in weekly.on_disk(path.parent.parent, path.name)]


def two_halves(env, monkeypatch, first="aaaaaaaaaaaa+dirty.11111111", second="aaaaaaaaaaaa+dirty.22222222"):
    """W02-W03 under one revision, W03 again under another: a run split by a strategy change."""
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: first)
    run(env, weeks_range(W02, W03))
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: second)
    run(env, weeks_range(W03, W03))
    meta = {run_dir.read_json(p, run_dir.META)["strategy_rev"]: p.name for p in folders(env)}
    return meta[first], meta[second]


def test_a_merge_needs_the_two_revisions_shown_to_be_one_source(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    with pytest.raises(ValueError, match="--unverified"):
        revisions.merge(env.work_dir, older, newer, apply=True)
    assert len(folders(env)) == 2  # nothing moved


def test_an_unverified_merge_joins_the_halves_and_says_so(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    assert revisions.merge(env.work_dir, older, newer, unverified=True) == 1  # a dry run counts
    assert len(folders(env)) == 2

    assert revisions.merge(env.work_dir, older, newer, unverified=True, apply=True) == 1
    target = run_dir.run_dir(env.work_dir, newer)
    assert weeks_of(target) == ["2026-W02", "2026-W03"]
    assert run_dir.is_complete(target)  # the root is rebuilt over both weeks
    copied = run_dir.read_json(run_dir.week_dir(env.work_dir, newer, "2026-W02"), run_dir.META)
    assert copied["merged_from"] == {"run_id": older, "strategy_rev": "aaaaaaaaaaaa+dirty.11111111", "verified": False}
    assert run_dir.read_json(target, run_dir.META)["merged"][0]["weeks"] == ["2026-W02"]
    # the source is set aside, not deleted, and no longer listed
    assert (target.parent / f"{older}{revisions.MERGED_SUFFIX}").is_dir()
    assert [p.name for p in run_dir.iter_complete(env.work_dir)] == [newer]


def test_a_merge_refuses_a_shared_week_that_differs(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    trades = run_dir.week_dir(env.work_dir, older, "2026-W03") / run_dir.TRADES
    trades.write_text(trades.read_text() + trades.read_text().splitlines()[-1] + "\n")
    with pytest.raises(ValueError, match="2026-W03 differs"):
        revisions.merge(env.work_dir, older, newer, unverified=True, apply=True)


def test_a_merge_of_one_source_under_two_names_needs_no_flag(make_env, monkeypatch):
    env = make_env("env")
    older, newer = two_halves(env, monkeypatch)
    monkeypatch.setattr(revisions, "source_of", lambda meta: "src.000000000000")
    revisions.merge(env.work_dir, older, newer, apply=True)
    copied = run_dir.read_json(run_dir.week_dir(env.work_dir, newer, "2026-W02"), run_dir.META)
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


def test_a_folder_recorded_at_a_commit_moves_to_the_source_revision_and_is_reused(make_env, monkeypatch):
    commit = _clean_commit()
    env = make_env("env")
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: commit)
    run(env, weeks_range(W02, W02))
    [old] = folders(env)
    recorded = run_dir.read_json(old, run_dir.META)["strategy_commit"]

    report = revisions.migrate(env.work_dir, apply=True)
    [new] = folders(env)
    assert report.renamed == [(old.name, new.name)]
    meta = run_dir.read_json(new, run_dir.META)
    assert meta["strategy_rev"] == strategy_rev("example.strategy", "v1")
    assert meta["strategy_commit"] == recorded and meta["id"] == new.name  # provenance kept as it was

    # A run of the same source today lands in that folder and replays nothing it holds.
    monkeypatch.undo()
    runner = run(env, weeks_range(W02, W03))
    assert [p.name for p in folders(env)] == [new.name]
    assert weeks_of(new) == ["2026-W02", "2026-W03"]
    assert runner is not None


def test_a_dirty_revision_is_left_as_it_is(make_env, monkeypatch):
    env = make_env("env")
    monkeypatch.setattr(enoshima.runner, "strategy_rev", lambda module, variant: "aaaaaaaaaaaa+dirty.11111111")
    run(env, weeks_range(W02, W02))
    report = revisions.migrate(env.work_dir, apply=True)
    assert not report.renamed and len(report.skipped) == 1
