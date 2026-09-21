"""结论对象 schema 校验器（P0-07 · 审计整改施工意见）。

把散落的 `if not track` 判断收成**一个正式校验器**：
`build_conclusion()` / `render_html()` 都必须复用它，校验不过一律拒绝产出报告，
绝不允许把损坏对象补全成「看起来正常」的报告（P4 失败显性化）。

校验项：
- track ∈ {commerce, research}，缺失/空/非法即拒绝（不再回落 commerce）
- provenance 四元组完整（D3：run_id × snapshot_id × batch_id × commit_sha）
- metric = {name, version}
- data = {snapshot_id, rows_scanned, series: [[period, value], ...]}
- claims 非空，每项 reconcile=PASS 且 source 三元组完整（ADR-18）
"""
from __future__ import annotations

from typing import Any

from core.audit.ledger import REQUIRED_PROVENANCE

TRACKS = ("commerce", "research")
REQUIRED_SOURCE = ("query_id", "run_id", "snapshot_id")


class ConclusionSchemaError(ValueError):
    """结论对象不符合 schema。"""


def validate_conclusion(conclusion: dict[str, Any]) -> None:
    """校验结论对象；不合法即抛 ConclusionSchemaError。"""
    if not isinstance(conclusion, dict):
        raise ConclusionSchemaError("conclusion must be a mapping")

    # —— track：缺失/空/非法一律拒绝（P0-07 核心）——
    track = conclusion.get("track")
    if not track:
        raise ConclusionSchemaError("conclusion.track 缺失（D10）：不得回落默认轨道")
    if track not in TRACKS:
        raise ConclusionSchemaError(f"conclusion.track 非法：{track!r}（只能是 {TRACKS}）")

    # —— provenance 四元组（D3 —— 含代码版本指纹）——
    prov = conclusion.get("provenance")
    if not isinstance(prov, dict):
        raise ConclusionSchemaError("conclusion.provenance 必须是映射")
    missing = [k for k in REQUIRED_PROVENANCE if not prov.get(k)]
    if missing:
        raise ConclusionSchemaError(f"conclusion.provenance 缺 {missing}（D3）")

    # —— metric ——
    metric = conclusion.get("metric")
    if not isinstance(metric, dict) or not metric.get("name"):
        raise ConclusionSchemaError("conclusion.metric 必须含非空 name")
    if not isinstance(metric.get("version", 1), int):
        raise ConclusionSchemaError("conclusion.metric.version 必须是整数")

    # —— data ——
    data = conclusion.get("data")
    if not isinstance(data, dict):
        raise ConclusionSchemaError("conclusion.data 必须是映射")
    if not data.get("snapshot_id"):
        raise ConclusionSchemaError("conclusion.data.snapshot_id 缺失")
    series = data.get("series") or []
    if not isinstance(series, list):
        raise ConclusionSchemaError("conclusion.data.series 必须是 [[period, value], ...]")
    for row in series:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ConclusionSchemaError(f"conclusion.data.series 行格式错误：{row!r}")

    # —— claims（ADR-18：无对账依据不得渲染）——
    claims = conclusion.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ConclusionSchemaError("conclusion.claims 必须是非空列表（ADR-18）")
    for cl in claims:
        if cl.get("reconcile") != "PASS":
            raise ConclusionSchemaError(
                f"claim 未通过对账：period={cl.get('period')!r} reconcile={cl.get('reconcile')!r}"
            )
        src = cl.get("source") or {}
        miss = [k for k in REQUIRED_SOURCE if not src.get(k)]
        if miss:
            raise ConclusionSchemaError(f"claim.source 缺 {miss}（period={cl.get('period')!r}）")


__all__ = ["validate_conclusion", "ConclusionSchemaError", "TRACKS"]
