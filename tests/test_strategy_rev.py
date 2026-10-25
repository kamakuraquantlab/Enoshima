"""A run's id carries its variant's source; the commit it ran from is recorded beside it."""

import subprocess

import pytest

from enoshima.strategy_env import (
    SOURCE_REV_PREFIX,
    UNVERSIONED,
    source_rev,
    sources_at_commit,
    strategy_commit,
    strategy_rev,
    variant_sources,
)

PKG = "strat_under_test"


def git(repo, *args) -> str:
    return (
        subprocess.check_output(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo)
        .decode()
        .strip()
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git checkout holding `strat_under_test`: v1 imports a helper three ways, v2 stands alone."""
    root = tmp_path / "strategy_repo"
    package = root / "src" / PKG
    (package / "parts").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "parts" / "__init__.py").write_text("")
    (package / "parts" / "bars.py").write_text("BAR = 1\n")
    (package / "common.py").write_text("from .parts import bars\nC = 1\n")
    (package / "risk.py").write_text("R = 1\n")
    (package / "v1.py").write_text(f"from .common import C\nimport {PKG}.risk\nfrom {PKG} import notes_reader\n")
    (package / "notes_reader.py").write_text("N = 1\n")
    (package / "v2.py").write_text("import json\nX = 2\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "first")
    monkeypatch.syspath_prepend(str(root / "src"))
    return root


def touch(repo, name, text):
    (repo / "src" / PKG / name).write_text(text)


def test_a_variants_source_is_its_module_and_what_it_imports_from_its_package(repo):
    assert sorted(variant_sources(PKG, "v1")) == [
        f"{PKG}/__init__.py",
        f"{PKG}/common.py",
        f"{PKG}/notes_reader.py",
        f"{PKG}/parts/__init__.py",
        f"{PKG}/parts/bars.py",
        f"{PKG}/risk.py",
        f"{PKG}/v1.py",
    ]
    assert sorted(variant_sources(PKG, "v2")) == [f"{PKG}/__init__.py", f"{PKG}/v2.py"]


def test_the_revision_is_the_source_whatever_the_commit(repo):
    rev = strategy_rev(PKG, "v1")
    assert rev.startswith(SOURCE_REV_PREFIX)
    touch(repo, "v3.py", "from .common import C\n")  # a new variant beside it
    touch(repo, "v2.py", "X = 3\n")  # another variant's change
    (repo / "notes.txt").write_text("last night\n")
    assert strategy_rev(PKG, "v1") == rev
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "an experiment")
    assert strategy_rev(PKG, "v1") == rev


def test_a_change_to_anything_the_variant_imports_is_a_new_revision(repo):
    rev = strategy_rev(PKG, "v1")
    touch(repo, "parts/bars.py", "BAR = 2\n")  # two imports away
    changed = strategy_rev(PKG, "v1")
    assert changed != rev
    touch(repo, "parts/bars.py", "BAR = 1\n")
    assert strategy_rev(PKG, "v1") == rev  # reverting returns to the first


def test_a_commits_source_reads_back_to_the_revision_a_run_of_it_gets(repo):
    commit = git(repo, "rev-parse", "HEAD")[:12]
    rev = strategy_rev(PKG, "v1")
    touch(repo, "risk.py", "R = 2\n")
    git(repo, "commit", "-q", "-am", "second")
    assert source_rev(sources_at_commit(PKG, "v1", commit)) == rev
    assert strategy_rev(PKG, "v1") != rev


def test_the_commit_is_recorded_dirty_or_clean(repo):
    clean = strategy_commit(PKG)
    assert clean == git(repo, "rev-parse", "HEAD")[:12]
    touch(repo, "v1.py", "X = 9\n")
    assert strategy_commit(PKG).startswith(clean + "+dirty.")


def test_a_strategy_outside_any_checkout_still_has_a_source_revision(tmp_path, monkeypatch):
    (tmp_path / "loose_strat").mkdir()
    (tmp_path / "loose_strat" / "__init__.py").write_text("")
    (tmp_path / "loose_strat" / "v1.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert strategy_rev("loose_strat", "v1").startswith(SOURCE_REV_PREFIX)
    assert strategy_commit("loose_strat") == UNVERSIONED
