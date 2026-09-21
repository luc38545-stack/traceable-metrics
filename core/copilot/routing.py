"""多模型路由表 + 面级约束前置过滤器（架构书 §04 L4 · Copilot 框架四件套之一）。

四件套：**多模型路由表 · 数字对账 diff · 交叉审核 · 人审升级**，全局唯一实现。

本模块负责「多模型路由表」与「面级约束」：
- 路由表：{face: {purpose: [模型名按优先级排列]}}。face=C 只见 business 口径集，
  face=R 只见 research 口径集 —— 约束在**路由前置过滤器**实施，不是各调用方自觉。
- 交叉审核（第二模型审第一模型）与人审升级：V1 **显式标注未启用**（P4：不静默），
  由 audit 字段暴露给消费端，等接入真实 Provider 后在同一框架内补齐。
"""
from __future__ import annotations

from typing import Any, Mapping

#: 面与轨的对应关系（stamping 一致性：ADR-14 的 face 字段）
FACE_TRACK: Mapping[str, str] = {"C": "commerce", "R": "research"}

#: 默认路由表：V1 每个 (face, purpose) 单模型。多模型交叉在 V1 未启用（见 audit）。
DEFAULT_ROUTES: Mapping[str, Mapping[str, list[str]]] = {
    "C": {"report": ["default"]},
    "R": {"report": ["default"]},
}


class FaceConstraintError(ValueError):
    """面级约束违反：face 与结论轨道不一致。"""


class RouteTable:
    """多模型路由表。V1 支持单模型路由 + 面级校验，多模型备用在表里预留。"""

    def __init__(self, routes: Mapping[str, Mapping[str, list[str]]] | None = None) -> None:
        self.routes = {face: dict(ps) for face, ps in (routes or DEFAULT_ROUTES).items()}

    def route(self, face: str, track: str, purpose: str = "report") -> dict[str, Any]:
        """路由（带前置过滤器）：face 与 track 必须一致，否则拒绝。

        返回首选模型名与完整候选（候选列表 V1 恒为单元素，交叉审核未启用）。
        """
        expect = FACE_TRACK.get(face)
        if expect is None:
            raise FaceConstraintError(f"未知 face：{face!r}（合法值 {sorted(FACE_TRACK)}）")
        if expect != track:
            raise FaceConstraintError(
                f"面级约束违反：face={face} 只见 {expect} 口径集，"
                f"但结论轨道是 {track}。路由前置过滤器拦截。"
            )
        candidates = self.routes.get(face, {}).get(purpose, [])
        if not candidates:
            raise FaceConstraintError(
                f"路由表里没有 face={face} / purpose={purpose} 的模型"
            )
        return {
            "face": face,
            "track": track,
            "purpose": purpose,
            "candidates": list(candidates),
            "primary": candidates[0],
            "cross_review": "not_enabled",   # 四件套之二：V1 显式未启用
            "human_escalation": "not_enabled",  # 四件套之四：V1 显式未启用
        }


__all__ = ["RouteTable", "FaceConstraintError", "FACE_TRACK", "DEFAULT_ROUTES"]
