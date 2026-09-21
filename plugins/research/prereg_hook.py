"""预注册执行闸门（S3 论文轨 · plugins/research 插件层）。

与 core/statistics/prereg.py 的分工：
- core 只管「登记」与「对账」——不 import 任何插件（C-1）；
- 本插件负责「论文轨执行必须先过闸」——这是轨道差异，必须走插件（C-2）。

闸门规则：
1. 执行的 plan_id 必须在预注册台账中登记过，否则 PreregistrationRequired（先登记后执行）；
2. 登记过的计划还要逐口径对账（assert_plan_match）——换方法/换 α/换 MDE 一律拦截。
"""
from __future__ import annotations

from typing import Any

from core.statistics.prereg import assert_plan_match


class PreregistrationRequired(RuntimeError):
    """论文轨执行缺少有效预注册。"""


def require_preregistered(ledger, plan_id: str, executed: dict[str, Any]) -> dict[str, Any]:
    """论文轨执行闸门：校验 plan_id 已登记且执行口径与计划一致。

    ledger: 预注册台账对象（须有 .get(plan_id)）。
    通过 → 返回登记的计划（供下游记录）；不通过 → 显式异常（P4，不静默降级）。
    """
    plan = ledger.get(plan_id) if ledger is not None else None
    if plan is None:
        raise PreregistrationRequired(
            f"论文轨执行必须携带已登记的预注册计划：{plan_id!r} 未在台账中"
            "（先登记计划，再执行分析——禁止事后补登记）。"
        )
    assert_plan_match(plan, executed)
    return plan
