"""An environment's strategy: its definition, its config, and the revision of the code that runs it.

## The strategy's revision is part of a run's id, and it is the variant's source

A run's numbers are the strategy code's as much as the parameters', so a run
id includes `strategy_rev`: a hash of the source the variant runs -- its own
module and every module of its package it imports, followed through the import
statements, with the `__init__.py` of each package on the way. It is
`src.<12 hex>`.

So a change to a variant's code, or to anything it imports from its package, is
a new folder, and a weekly run never mixes weeks of two versions of it. A
change that does not reach it -- a new variant beside it, another variant's
fix, a test, a note -- leaves its folder where it is, committed or not. That
is the point: a strategy repository that keeps every experiment as its own
`vNNN.py` gains a file with every experiment, and an id taken from the commit
moved every older variant's results to a new folder each time.

What is not in it: libraries outside the package (qate and the rest), which
were never part of the revision, and a module imported dynamically, by a
string, which the import statements do not name.

The commit it ran from is still recorded, in `meta.json` as `strategy_commit`:
`<sha>`, `<sha>+dirty.<hash>` with uncommitted changes, or the distribution's
`name==version`. It is provenance, not identity. It was the revision once, and
folders written then carry a commit as their `strategy_rev`; `enoshima
migrate-revisions` gives them their source revision, read from that commit.
"""

import ast
import hashlib
import importlib.metadata
import importlib.util
import subprocess
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path

from qate.trading.config import StrategyConfig
from qate_env.env import Env
from qate_env.trading_env import get_strategy_class, resolve_config_class

LOG = getLogger(__name__)

UNVERSIONED = "unversioned"
SOURCE_REV_PREFIX = "src."

STRATEGY_CONFIG_FILE = "strategy_config.json"
STRATEGY_PARAM_GRID_FILE = "strategy_param_grid.json"


@dataclass(frozen=True)
class StrategyParamGrid:
    module_name: str
    variant_name: str
    params: list[dict] = field(default_factory=list)

    @property
    def strategy_name(self) -> str:
        return f"{self.module_name}.{self.variant_name}"


@dataclass(frozen=True)
class StrategyEnv:
    definition: StrategyParamGrid
    config: StrategyConfig


def load_strategy_env(env: Env) -> StrategyEnv:
    definition = env.load_object(STRATEGY_PARAM_GRID_FILE, StrategyParamGrid)
    if definition is None:
        raise RuntimeError(f"No {STRATEGY_PARAM_GRID_FILE} in {env.work_dir}")
    if not definition.module_name:
        raise RuntimeError(f"{STRATEGY_PARAM_GRID_FILE} in {env.work_dir} names no module_name")
    if not definition.variant_name:
        raise RuntimeError(f"{STRATEGY_PARAM_GRID_FILE} in {env.work_dir} names no variant_name")

    config = env.load_object(STRATEGY_CONFIG_FILE, resolve_config_class(definition.module_name))
    if config is None:
        raise RuntimeError(f"No {STRATEGY_CONFIG_FILE} in {env.work_dir}")
    return StrategyEnv(definition, config)


def _git(cwd: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL)


def package_base(module_name: str) -> Path:
    """The directory the strategy's top-level package sits in: module paths are relative to it."""
    top = module_name.split(".")[0]
    spec = importlib.util.find_spec(top)
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(f"Cannot find the strategy package {top}")
    return Path(list(spec.submodule_search_locations)[0]).parent


def variant_sources(module_name: str, variant_name: str, read=None) -> dict[str, bytes]:
    """The source a variant runs: path, relative to `package_base`, to bytes.

    Its module and every module of its package it imports, followed through
    `import` and `from ... import` statements, relative or absolute, and the
    `__init__.py` of every package on the way. `read(relative_path)` returns a
    file's bytes or None; by default the files on disk, and `sources_at_commit`
    reads them from a commit instead.
    """
    top = module_name.split(".")[0]
    if read is None:
        base = package_base(module_name)

        def read(relative: str) -> bytes | None:
            path = base / relative
            return path.read_bytes() if path.is_file() else None

    found: dict[str, bytes] = {}

    def module_file(name: str) -> tuple[str, bytes] | None:
        stem = name.replace(".", "/")
        for relative in (f"{stem}.py", f"{stem}/__init__.py"):
            data = read(relative)
            if data is not None:
                return relative, data
        return None

    queue, seen = [f"{module_name}.{variant_name}"], set()
    while queue:
        name = queue.pop()
        if name in seen or not (name == top or name.startswith(top + ".")):
            continue
        seen.add(name)
        parts = name.split(".")
        queue.extend(".".join(parts[:k]) for k in range(1, len(parts)))  # its packages' __init__.py
        located = module_file(name)
        if located is None:
            continue  # a name imported from a module, not a module of its own
        relative, data = located
        found[relative] = data
        package = parts if relative.endswith("__init__.py") else parts[:-1]
        for node in ast.walk(ast.parse(data, filename=relative)):
            if isinstance(node, ast.Import):
                queue.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    stem = package[: len(package) - node.level + 1]
                    target = ".".join(stem + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                queue.append(target)
                queue.extend(f"{target}.{alias.name}" for alias in node.names)
    if f"{module_name.replace('.', '/')}/{variant_name}.py" not in found:
        raise RuntimeError(f"Cannot find the source of {module_name}.{variant_name}")
    return found


def source_rev(sources: dict[str, bytes]) -> str:
    """`src.<hash>` of a variant's source, path by path."""
    digest = hashlib.sha256()
    for relative in sorted(sources):
        digest.update(relative.encode() + b"\0" + sources[relative] + b"\0")
    return SOURCE_REV_PREFIX + digest.hexdigest()[:12]


def strategy_rev(module_name: str, variant_name: str) -> str:
    """The revision a run of this variant is identified by; see the module docstring."""
    return source_rev(variant_sources(module_name, variant_name))


def sources_at_commit(module_name: str, variant_name: str, commit: str) -> dict[str, bytes]:
    """A variant's source as it was at `commit` of the checkout its package is imported from."""
    base = package_base(module_name)
    top = Path(_git(base, "rev-parse", "--show-toplevel").decode().strip())
    prefix = base.resolve().relative_to(top.resolve())

    def read(relative: str) -> bytes | None:
        try:
            return _git(top, "show", f"{commit}:{(prefix / relative).as_posix()}")
        except subprocess.CalledProcessError:
            return None

    return variant_sources(module_name, variant_name, read)


def strategy_commit(module_name: str) -> str:
    """The commit the code `module_name` is imported from, `+dirty.<hash>` if it has uncommitted changes.

    Recorded with a run as its provenance; see the module docstring.
    """
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"Cannot find the strategy module {module_name}")
    where = Path(spec.origin).parent
    try:
        top = Path(_git(where, "rev-parse", "--show-toplevel").decode().strip())
        head = _git(top, "rev-parse", "HEAD").decode().strip()[:12]
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return _installed_version(module_name)

    digest = hashlib.sha256()
    diff = _git(top, "diff", "HEAD", "--binary")
    digest.update(diff)
    untracked = sorted(
        f for f in _git(top, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
        if f.endswith(".py")
    )
    for name in untracked:
        digest.update(name.encode() + b"\0" + (top / name).read_bytes() + b"\0")
    if not diff and not untracked:
        return head
    return f"{head}+dirty.{digest.hexdigest()[:8]}"


def _installed_version(module_name: str) -> str:
    top = module_name.split(".")[0]
    for dist in importlib.metadata.packages_distributions().get(top, []):
        return f"{dist}=={importlib.metadata.version(dist)}"
    LOG.warning(f"{module_name} is neither in a git checkout nor installed from a distribution: its runs "
                f"cannot tell one version of it from another")
    return UNVERSIONED


__all__ = [
    "STRATEGY_CONFIG_FILE",
    "STRATEGY_PARAM_GRID_FILE",
    "StrategyEnv",
    "StrategyParamGrid",
    "get_strategy_class",
    "load_strategy_env",
    "source_rev",
    "sources_at_commit",
    "strategy_commit",
    "strategy_rev",
    "variant_sources",
]
