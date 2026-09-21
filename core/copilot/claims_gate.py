"""D6 数字对账铁律（架构书 §04 · Copilot 框架 · 对账只认 claims）。

架构书原话：「所有进入报告管线的 LLM 产物必须双形态输出——自然语言正文 + claims 数组。
对账只认 claims：每个 claim 的 source 必须指向一次真实语义查询，逐字段 diff，
PASS 才渲染、FAIL 即拦截并出对账失败单。」（ADR-18 / P0-2 盲区闭合）

本模块把这条铁律落成可执行的闸门，核心是 **NumberBank（数字银行）**：

- 数字银行来自统计执行器的 claims（已带 source 三元组，已过 validate_claim）。
- LLM 生成正文时**只允许引用数字银行的 key**（``citations``），不直接发明数字；
  最终渲染的数值一律由系统从银行注入 —— 幻觉面在构造上被压到最小（C-5）。
- 闸门两件事：① citations 必须都在银行里；② 正文中出现的每个数字
  （小数 / 百分比）必须能在被引用 claim 的值里找到**精确匹配**，否则 FAIL。

诚实边界（写进测试）：数字提取器只抓「小数」和「百分比」两类 token。
裸整数（如「第2名」「2026 年」）不抓 —— 统计结论的数字几乎总是小数/百分比形态，
V1.1 再做单位感知提取。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

#: 占位符引用：{claim:metric#period}，渲染时由系统填真实值
PLACEHOLDER_RE = re.compile(r"\{claim:([^}]+)\}")
#: 数字 token：可选负号 + 数字（可选千分位/小数）+ 可选 %
NUMBER_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?")


class NumberBank:
    """数字银行：从结论 claims 建索引，是 D6 对账的唯一事实源。"""

    def __init__(self, claims: list[dict[str, Any]]) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self._values: list[float] = []
        for c in claims:
            key = f"{c['metric']}#{c['period']}"
            self.items[key] = c
            self._values.append(float(c["value"]))

    def __bool__(self) -> bool:
        return bool(self.items)

    def get(self, key: str) -> dict[str, Any] | None:
        return self.items.get(key)

    def has_value(self, v: float) -> bool:
        """银行里是否存在与 v 精确一致的值（D6：逐字段一致，不留容差）。"""
        return any(abs(v - x) < 1e-9 for x in self._values)

    def closest(self, v: float) -> float | None:
        if not self._values:
            return None
        return min(self._values, key=lambda x: abs(x - v))


def _parse_number(tok: str) -> float:
    """把 token 解析成浮点：去千分位；百分比除以 100（6% → 0.06）。"""
    t = tok.replace(",", "")
    if t.endswith("%"):
        return float(t[:-1]) / 100.0
    return float(t)


def extract_numbers(text: str) -> list[str]:
    """提取正文里的「对账相关」数字：小数或百分比。裸整数忽略（见模块 docstring）。"""
    out = []
    for m in NUMBER_RE.finditer(text):
        tok = m.group(0)
        if "." in tok or "%" in tok:
            out.append(tok)
    return out


def gate_narrative(
    narrative: str,
    citations: list[str],
    bank: NumberBank,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """D6 闸门：citations 合法性 + 正文数字对账。任何 FAIL → 正文不得渲染。

    strict（默认 True，架构语义）：正文每个数字必须与**被引用** claim 的值
    逐位一致——citations 为空或有数字落在未引用 claim 上 → FAIL。
    strict=False 仅保留「匹配银行内任意值」的历史宽松行为（仅供老调用方
    显式降级；独立闸门 CLI 与解释管线一律 strict）。
    """
    # ① citations 必须都指向银行里的真实 claim
    bad_citations = [k for k in citations if k not in bank.items]

    # ② 正文数字对账：每个数字必须精确匹配某条被引用 claim 的值
    unmatched: list[dict[str, Any]] = []
    cited_values = set()
    for k in citations:
        c = bank.get(k)
        if c is not None:
            cited_values.add(float(c["value"]))

    def _matches(v: float) -> bool:
        return any(abs(v - x) < 1e-9 for x in cited_values) or (
            not strict and bank.has_value(v)
        )

    for m in NUMBER_RE.finditer(narrative):
        tok = m.group(0)
        if "." not in tok and "%" not in tok:
            continue
        v = _parse_number(tok)
        if not _matches(v):
            start = max(0, m.start() - 24)
            end = min(len(narrative), m.end() + 24)
            unmatched.append({
                "number": tok,
                "context": narrative[start:end].replace("\n", " "),
                "closest_bank_value": bank.closest(v),
            })

    failed = [*[{"citation": k} for k in bad_citations], *unmatched]
    return {
        "status": "PASS" if not failed else "FAIL",
        "bad_citations": bad_citations,
        "unmatched_numbers": unmatched,
        "failed": failed,
    }


def render_narrative(narrative: str, bank: NumberBank) -> str:
    """把 {claim:key} 占位符替换为银行里的真实数值（系统注入，LLM 不碰数字）。"""

    def _fmt_value(v: float, unit: str) -> str:
        if unit in ("diff_proportion", "ratio", "proportion", "pct", "rate"):
            return f"{v:.2%}"
        return f"{v:g}"

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        c = bank.get(key)
        if c is None:
            return m.group(0)  # 引用不存在：保留原样（闸门已拦住，正常到不了这）
        return _fmt_value(float(c["value"]), c.get("unit", ""))

    return PLACEHOLDER_RE.sub(_repl, narrative)


__all__ = ["NumberBank", "extract_numbers", "gate_narrative", "render_narrative"]
