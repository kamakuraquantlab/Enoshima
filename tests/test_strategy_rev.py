"""A run's id carries the revision of the strategy's own repository, uncommitted changes included."""

import subprocess

import pytest

from enoshima.strategy_env import UNVERSIONED, strategy_rev


def git(repo, *args) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo
    ).decode().strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git checkout holding the package `strat_under_test`, importable."""
    root = tmp_path / "strategy_repo"
    package = root / "strat_under_test"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "v1.py").write_text("X = 1\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    monkeypatch.syspath_prepend(str(root))
    return root


def test_a_clean_checkout_is_its_commit(repo):
    assert strategy_rev("strat_under_test") == git(repo, "rev-parse", "HEAD")[:12]


def test_a_new_commit_is_a_new_revision(repo):
    first = strategy_rev("strat_under_test")
    (repo / "strat_under_test" / "v1.py").write_text("X = 2\n")
    git(repo, "commit", "-q", "-am", "second")
    assert strategy_rev("strat_under_test") != first


def test_uncommitted_changes_are_a_revision_of_their_own_and_reverting_them_is_not(repo):
    clean = strategy_rev("strat_under_test")
    source = repo / "strat_under_test" / "v1.py"
    source.write_text("X = 2\n")
    two = strategy_rev("strat_under_test")
    source.write_text("X = 3\n")
    three = strategy_rev("strat_under_test")
    assert two.startswith(clean + "+dirty.") and three.startswith(clean + "+dirty.")
    assert two != three
    source.write_text("X = 1\n")
    assert strategy_rev("strat_under_test") == clean


def test_only_untracked_python_counts(repo):
    clean = strategy_rev("strat_under_test")
    (repo / "notes.txt").write_text("results of last night\n")
    assert strategy_rev("strat_under_test") == clean
    (repo / "strat_under_test" / "helper.py").write_text("Y = 1\n")
    assert strategy_rev("strat_under_test") != clean


def test_a_strategy_outside_any_checkout_is_unversioned(tmp_path, monkeypatch):
    (tmp_path / "loose_strat").mkdir()
    (tmp_path / "loose_strat" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert strategy_rev("loose_strat") == UNVERSIONED
