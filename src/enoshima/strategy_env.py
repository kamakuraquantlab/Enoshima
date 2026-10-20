"""An environment's strategy: its definition, its config, and the revision of the code that runs it.

## The strategy's revision is part of a run's id

A run's numbers are the strategy code's as much as the parameters', so a run
id includes `strategy_rev`: the commit of the git checkout the strategy module
is imported from -- the strategy's own repository, not Enoshima's or qate's.
A commit gives a new folder, and a weekly run never mixes weeks of two
versions of the strategy.

Uncommitted changes are a version too: `<sha>+dirty.<hash>`, the hash of the
diff against HEAD and of the untracked `.py` files. Reverting them returns to
the clean revision's folder. Untracked files that are not Python -- data, notes,
output -- do not count, so a file written into the checkout does not move every
run to a new folder.

A strategy installed rather than checked out is its distribution's
`name==version`; one that is neither is `unversioned`, and a warning says
that its runs cannot tell one version of it from another.
"""

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


def strategy_rev(module_name: str) -> str:
    """The revision of the code `module_name` is imported from; see the module docstring."""
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
    "strategy_rev",
]
