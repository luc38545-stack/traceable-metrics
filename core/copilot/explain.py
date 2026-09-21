"""LLM 解释生成器（架构书 V1-DoD 技术侧末项：「LLM 解释(claims 过闸)」）。

流程（六步，任何一步不过就出失败单，**不许带病渲染**）：

  conclusion(带 claims) → ① 数字银行 → ② 路由面级校验 → ③ prompt(含数字银行+口径)
  → ④ LLM 输出 {narrative, citations} → ⑤ D6 数字对账闸门 → ⑥ 双形态产物
  {narrative, claims}，claims 由系统从银行注入（LLM 不碰数字，C-5 构造性满足）。

审计标注（P4 不静默）：交叉审核 / 人审升级 / 多模型 在 V1 显式标「未启用」，
由 audit 字段暴露，消费端可据此在报告里展示「本解释未经交叉审核」。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from core.copilot.claims_gate import (
    NumberBank,
    gate_narrative,
    render_narrative,
)
from core.copilot.provider import LLMProvider, NullProvider, ProviderNotConfigured
from core.copilot.routing import FaceConstraintError, RouteTable


class ExplainGateError(RuntimeError):
    """LLM 解释被闸门拦截（含失败单）。调用方必须显式处理，不能带病渲染。"""


def build_bank(conclusion: Mapping[str, Any]) -> NumberBank:
    """数字银行 = 结论对象的 claims（已带 source 三元组的可信数字）。"""
    return NumberBank(list(conclusion.get("claims") or []))


def _bank_prompt(bank: NumberBank) -> str:
    lines = []
    for key, c in bank.items.items():
        lines.append(
            f'- key="{key}"  metric={c["metric"]}  period={c["period"]}  '
            f'value={c["value"]}  unit={c["unit"]}'
        )
    return "\n".join(lines)


def build_prompt(
    bank: NumberBank,
    conclusion: Mapping[str, Any],
    *,
    face: str,
    question: str = "",
) -> str:
    """构造给 LLM 的 prompt。硬约束三条：

    1. 正文里要引用数字，只能写占位符 {claim:key}（key 必须是数字银行里的）。
    2. 若你坚持直接写数字，必须与被引用 claim 的值**逐位一致**（D6，抄错即拦截）。
    3. 输出严格 JSON：{{"narrative": "正文", "citations": ["key1", ...]}}。
    """
    status = conclusion.get("status", "?")
    q = question or conclusion.get("question") or "（未提供问题描述）"
    return (
        "你是 TraceableMetrics 平台的报告解释 Copilot（face={face}）。\n"
        "任务：把下面的统计结论写成给业务决策者看的人话解释。\n\n"
        "【结论状态】{status}\n"
        "【待解释的问题】{q}\n\n"
        "【数字银行（唯一可信数字来源，全部已过对账闸门）】\n"
        "{bank}\n\n"
        "【硬约束】\n"
        "1. 引用数字必须用占位符 {{{{claim:key}}}}，key 必须来自数字银行，不得自造。\n"
        "2. 如果直接写数字，必须与银行值逐位一致（D6 数字对账：抄错一个字就拦截）。\n"
        "3. 不许发明银行里不存在的指标、时期或数字。\n"
        "4. 状态为 RED 时，只能说「前提不满足、当前数据不足以判断」，不许给任何数字。\n"
        "5. 输出严格 JSON，不要 markdown 代码块：\n"
        '   {{"narrative": "正文", "citations": ["key1", "key2"]}}\n'
    ).format(
        face=face, status=status, q=q, bank=_bank_prompt(bank)
    )


def _parse_llm_output(raw: str) -> dict[str, Any]:
    """解析 LLM 的 JSON 输出。严格模式：解析失败 = 失败，不猜。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExplainGateError(f"LLM 输出不是合法 JSON（{e}）：{raw[:200]!r}") from e
    if not isinstance(obj, dict) or "narrative" not in obj:
        raise ExplainGateError(f"LLM 输出缺 narrative 字段：{raw[:200]!r}")
    citations = obj.get("citations") or []
    if not isinstance(citations, list) or not all(isinstance(k, str) for k in citations):
        raise ExplainGateError("LLM 输出的 citations 必须是字符串数组")
    return {"narrative": str(obj["narrative"]), "citations": citations}


def explain(
    conclusion: Mapping[str, Any],
    provider: LLMProvider | None = None,
    *,
    face: str = "C",
    question: str = "",
    routes: RouteTable | None = None,
) -> dict[str, Any]:
    """生成一份「已过对账闸门」的 LLM 解释（双形态：正文 + claims 数组）。

    失败路径全部显式抛异常（带失败单），调用方必须处理，不许静默带病渲染。
    """
    # ① 数字银行：没有 claims 就没有对账依据（C-5：无依据不出解释）
    bank = build_bank(conclusion)
    if not bank:
        raise ExplainGateError("结论没有 claims：无对账依据，不出 LLM 解释（C-5）。")

    # ② 路由面级校验（前置过滤器）
    track = conclusion.get("track", "")
    rt = routes or RouteTable()
    route = rt.route(face, track)

    # ③ Provider（未配置 → 显式失败）
    if provider is None:
        provider = NullProvider()

    # ④ prompt + 调用
    prompt = build_prompt(bank, conclusion, face=face, question=question)
    try:
        raw = provider.complete(prompt)
    except ProviderNotConfigured:
        raise  # 显式失败，由调用方决定展示「LLM 解释未启用」

    # ⑤ 解析 + D6 对账闸门
    out = _parse_llm_output(raw)
    gate = gate_narrative(out["narrative"], out["citations"], bank)
    if gate["status"] != "PASS":
        raise ExplainGateError(
            "LLM 解释未过 D6 数字对账闸门，正文不得进入渲染管线。"
            f"失败单：{json.dumps(gate['failed'], ensure_ascii=False)}"
        )

    # ⑥ 双形态产物：claims 由系统从银行注入（LLM 不碰数字）
    claims = [dict(bank.get(k)) for k in out["citations"]]
    narrative = render_narrative(out["narrative"], bank)

    return {
        "narrative": narrative,
        "claims": claims,
        "audit": {
            "provider": getattr(provider, "name", "unknown"),
            "route": route,
            "gate": "PASS",
            "gate_detail": gate,
            "audit_level": "L1-single-model",   # ADR-17：辅助级，单模型
            "cross_review": route["cross_review"],
            "human_escalation": route["human_escalation"],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    }


__all__ = ["explain", "build_bank", "build_prompt", "ExplainGateError"]
