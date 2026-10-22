"""环境变量装载、Root Agent 运行参数与费用登记。

模型 ID 由 agents_oagent.model_provider.MODEL_ID 决定；本模块提供温度与生成长度上限。
PRICING 以人民币每百万 token 登记；UNKNOWN_COST_MODELS 优先，费用未知时记 null。
费用登记仅用于记账，不限制模型启动。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent

DEFAULT_ENV_PATH = _PKG_DIR.parent / 'agents_oagent' / '.env.local'


def load_env(env_path: Path | None = None) -> list[str]:
    """把 .env.local 键值装入 os.environ（已存在的环境变量不覆盖）。返回装入的键名。"""
    path = env_path or DEFAULT_ENV_PATH
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def relay_base_url(env_path: Path | None = None) -> str:
    load_env(env_path)
    return os.environ['OAGENT_RELAY_BASE_URL'].rstrip('/')


def relay_api_key(env_path: Path | None = None) -> str:
    """key 只在内存流转，不得打印、落盘或进任何证据件。"""
    load_env(env_path)
    return os.environ['OAGENT_RELAY_API_KEY']


@dataclass(frozen=True)
class ModelSpec:
    """现行 Root Agent 角色的模型映射（运行参数单点）。"""

    role: str
    temperature: float
    max_tokens: int


# Root Agent 的温度与生成长度上限。
MODEL_MAP: dict[str, ModelSpec] = {
    'root_agent': ModelSpec(
        role='root_agent', temperature=0.2, max_tokens=16384,
    ),
}


@dataclass(frozen=True)
class Pricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float


PRICING: dict[str, Pricing] = {
    'deepseek-v4-flash-0731': Pricing(3.0, 9.0, 0.3),
    'qwen3.8-flash': Pricing(0.8, 2.7, 0.1),
    'qwen3.7-max': Pricing(12.0, 36.0, 2.4),
    'GLM-5.3-FlashX': Pricing(2.0, 7.0, 0.57),
}

UNKNOWN_COST_MODELS: dict[str, dict] = {
    'tierflow_pro': {
        'cost_mode': 'unknown',
        'basis': '路由模型的人民币单价未核准',
        'card': '费用登记：路由模型单价未知',
    },
    'tierflow': {
        'cost_mode': 'unknown',
        'basis': '套餐路由无统一 per-token 单价，不能按底层模型价格估算实扣',
        'card': '费用登记：路由模型单价未知',
    },
    'deepseek-v4-flash-0731': {
        'cost_mode': 'unknown',
        'basis': '端点人民币单价未核准，不沿用其他端点的同名模型价格',
        'card': '费用登记：端点单价未知',
    },
    'deepseek-v4-flash': {
        'cost_mode': 'unknown',
        'basis': 'Ark 编程套餐对 '
                 'deepseek-v4-flash 无可核对的人民币单价',
        'card': '费用登记：套餐单价未知',
    },
}
