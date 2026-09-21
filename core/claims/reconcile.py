"""Claim Schema 与对账（附录 T 冻结项⑥ / ADR-18）。

架构书要求：
- claims 的 source 必须指向一次真实语义查询（query_id/run_id/snapshot_id 三元组）
- 对账只认 claims：按 query 重放取实际值，逐字段 diff，PASS 才渲染、
  FAIL 即拦截并出对账失败单
- 负向：人为改 claim.value 一个字 → FAIL 且报告不渲染
"""
from __future__ import annotations

import math
from typing import Any

REQUIRED_SOURCE = ("query_id", "run_id", "snapshot_id")


class ClaimError(ValueError):
    """claim 不合法。"""


def range_from_contract(contract: dict[str, Any]) -> tuple[float, float] | None:
    """P0-01：合法范围由 Metric Contract 声明（如 `value_range: [0, 1]`）。

    通用核心不写死任何业务范围——契约没声明就不做范围校验，交给契约说话。
    """
    rng = contract.get("value_range")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return None
    lo, hi = float(rng[0]), float(rng[1])
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo > hi:
        raise ClaimError(f"contract value_range invalid: {rng!r}")
    return (lo, hi)


def validate_claim(
    claim: dict[str, Any],
    unit_range: tuple[float, float] | None = None,
) -> None:
    """校验单个 claim：必填 metric/period/value/unit + source 三元组。

    P0-01 严格类型约束（失败一律 ClaimError，绝不包装成 PASS）：
    - 类型上只接受 `int` / `float`：字符串即便能转成数字也拒绝——口径一旦靠
      「看起来像数字」来放行，就没人说得清 0.875 和 '0.875' 是不是一回事；
    - `NaN` 与任何值比较都为 False，会让 `abs(nan - replay) >= 1e-9` 恒为
      False 而被误判 PASS，静默绕过对账；
    - 显式拒绝 `Infinity / -Infinity`；
    - 显式拒绝 `bool`（`float(True) == 1.0`，不拒绝就会混进对账）；
    - 可选 `unit_range`：范围来自 Metric Contract 声明，核心不写死。
    """
    for f in ("metric", "period", "value", "unit"):
        if f not in claim:
            raise ClaimError(f"claim missing field: {f}")
    v = claim["value"]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ClaimError(
            f"claim value must be real number (int/float), got "
            f"{type(v).__name__}: {v!r}"
        )
    v = float(v)
    if not math.isfinite(v):
        raise ClaimError(f"claim value not finite: {claim['value']!r}")
    if unit_range is not None:
        lo, hi = unit_range
        if not (lo <= v <= hi):
            raise ClaimError(f"claim value out of contract range [{lo}, {hi}]: {v!r}")
    src = claim.get("source") or {}
    missing = [k for k in REQUIRED_SOURCE if not src.get(k)]
    if missing:
        raise ClaimError(f"claim source missing {missing} (source 三元组强制)")


def reconcile(
    claims: list[dict[str, Any]],
    replay: dict[str, float],
    unit_range: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """claims 与语义层重放值逐字段对账。

    replay: {period: value}，必须来自**已解析的精确快照重放**（P0-02 的
    SourceResolver 产出）；调用方不得自行构造事实源。
    返回逐 claim 的 PASS/FAIL 与整体 diff 结论；任何 FAIL → 报告不得渲染。
    """
    results = []
    for claim in claims:
        validate_claim(claim, unit_range=unit_range)
        raw = replay.get(claim["period"])
        expected = None if raw is None else float(raw)
        # 审计 #1：快照重放值 NaN/Inf 不得静默 PASS——必须显式 FAIL
        if expected is None or not math.isfinite(expected):
            results.append({**claim, "reconcile": "FAIL",
                            "reason": "replay missing or non-finite"})
        elif abs(expected - float(claim["value"])) >= 1e-9:
            results.append({**claim, "reconcile": "FAIL"})
        else:
            results.append({**claim, "reconcile": "PASS"})
    failed = [r for r in results if r["reconcile"] == "FAIL"]
    return {
        "claims": results,
        "reconcile_diff": "clean" if not failed else "MISMATCH",
        "quality_gate": "passed" if not failed else "blocked",
        "failed": failed,
    }
