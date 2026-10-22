# -*- coding: utf-8 -*-
"""构造 Root Agent 的 OpenAI 兼容 Chat Completions 模型。

凭据与端点通过 core.config 从环境变量或 agents_oagent/.env.local 装载。
模型 ID 与推理强度在本模块维护，温度与生成长度从 MODEL_MAP 读取。
关闭 SDK tracing，运行事件与用量由服务层记录；费用未知不阻止模型装配。"""
from __future__ import annotations

from pathlib import Path

from agents import ModelSettings, OpenAIChatCompletionsModel, set_tracing_disabled
from agents.models.interface import Model
from openai import AsyncOpenAI
from openai.types.shared.reasoning import Reasoning

from core import config

MODEL_ID = 'deepseek-v4-flash'
ROLE = 'root_agent'

REASONING_EFFORT = 'low'


def model_spec(env_path: Path | None = None) -> config.ModelSpec:
    """运行参数单点（temperature / max_tokens）；模型 id 由 MODEL_ID 决定。

    **费用表不是启动闸**（2026-10-22）：单价登记在 `config.PRICING` / `config.UNKNOWN_COST_MODELS`
    只用于记账（`core/usage_ledger.py`），模型没有登记单价时照常启动，费用未知记 `cost_cny=null`。
    """
    return config.MODEL_MAP[ROLE]


def build_model(env_path: Path | None = None) -> Model:
    """Chat Completions 路径直连中转站：AsyncOpenAI(base_url=relay, api_key=env)。"""
    set_tracing_disabled(True)
    return OpenAIChatCompletionsModel(
        model=MODEL_ID,
        openai_client=AsyncOpenAI(base_url=config.relay_base_url(env_path),
                                 api_key=config.relay_api_key(env_path)),
    )


def model_settings() -> ModelSettings:
    """温度 / 生成长度上限 / 推理强度（`REASONING_EFFORT`）。

    reasoning 只带 effort：SDK Chat Completions 路径只序列化 `reasoning.effort` 为
    `reasoning_effort`，mode/context 属 Responses API 语义、会被忽略（SDK 0.22.3
    `_handle_unsupported_reasoning_settings`）。

    tool_choice / parallel_tool_calls / stream_options 等易踩字段留空：探针 A–E 实测在
    非官方 client 下 SDK 默认不发、relay 零拒绝（README §三.7 也不要求发
    parallel_tool_calls=False——串行由 RunConfig.tool_execution 承担）。
    """
    spec = model_spec()
    return ModelSettings(temperature=spec.temperature, max_tokens=spec.max_tokens,
                         reasoning=Reasoning(effort=REASONING_EFFORT))
