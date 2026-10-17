from dataclasses import dataclass, field

from qate.trading.config import StrategyConfig
from qate_env.env import Env
from qate_env.trading_env import get_strategy_class, resolve_config_class

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


__all__ = [
    "STRATEGY_CONFIG_FILE",
    "STRATEGY_PARAM_GRID_FILE",
    "StrategyEnv",
    "StrategyParamGrid",
    "get_strategy_class",
    "load_strategy_env",
]
