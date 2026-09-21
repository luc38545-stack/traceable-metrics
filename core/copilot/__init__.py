"""Copilot 框架（共享核心 · L4 消费层）· LLM 解释腿 V1。

四件套全局唯一实现：多模型路由表 · 数字对账 diff(D6) · 交叉审核 · 人审升级。
V1（ADR-17 辅助级 L1）只启用前两件；后两件在 audit 字段里**显式标注未启用**（P4）。
"""
from core.copilot.claims_gate import (
    NumberBank,
    extract_numbers,
    gate_narrative,
    render_narrative,
)
from core.copilot.explain import ExplainGateError, explain
from core.copilot.provider import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MODEL,
    LLMProvider,
    NullProvider,
    OpenAICompatibleProvider,
    ProviderNotConfigured,
    explain_status,
)
from core.copilot.routing import (
    DEFAULT_ROUTES,
    FACE_TRACK,
    FaceConstraintError,
    RouteTable,
)

__all__ = [
    "explain", "ExplainGateError", "explain_status",
    "NumberBank", "extract_numbers", "gate_narrative", "render_narrative",
    "LLMProvider", "NullProvider", "OpenAICompatibleProvider",
    "ProviderNotConfigured",
    "RouteTable", "FaceConstraintError", "FACE_TRACK", "DEFAULT_ROUTES",
    "ENV_BASE_URL", "ENV_API_KEY", "ENV_MODEL",
]
