"""Claim source 强绑定解析器（P0-02 · 审计整改施工意见）。

对账前必须**证明** claim 的 source 三元组真实存在且互相对应，而不是只看字段非空：

1. `query_id` 必须存在于台账 `queries` 表；
2. 该 query 的 `via_semantic = 1`（绕过语义层的取数不算可信来源）；
3. query 的 `run_id / track / metric` 必须分别等于 claim 与当前结论对象；
4. run 台账中的 `snapshot_id` 必须等于 claim 的 source；
5. 快照文件必须存在，**实时 SHA-256 必须等于 run 台账记录的 snapshot_sha256**；
6. 对账只能从该精确快照重放，不接受调用方传入 `{period: value}` 作为事实源；
7. 任一环不闭合 → 写 append-only 审计失败事件，且报告禁止渲染（P4 失败显性化）。

路径全部由调用方注入（C-4：core 不选择数据位置）。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.audit import ledger
from core.claims.reconcile import ClaimError, range_from_contract, reconcile

# P1-06 联动：SQL 标识符严格规则（指标名/视图名只允许这个形状）
IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")


class SourceProofError(ClaimError):
    """source 关系无法被证明（伪造/交叉绑定/快照被替换）。"""


@dataclass(frozen=True)
class SourceRequest:
    """一次对账的溯源请求：全部位置信息由 app 层注入。"""

    ledger_db: Path        # 台账库（queries / runs 表）
    snapshot_file: Path    # 精确快照文件（由 run 台账的 snapshot_id 推出）
    query_id: str
    run_id: str
    snapshot_id: str
    track: str
    metric: str


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _open_ro(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SourceProofError(f"台账库不存在，无法证明 source：db={db.name}")
    return sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)


def prove_source(req: SourceRequest) -> dict[str, Any]:
    """逐环证明 source，返回 `{"replay": {period: value}, "checks": [...]}`。

    任一环不闭合即抛 SourceProofError——绝不回退、绝不用别的快照顶替。
    """
    checks: list[dict[str, Any]] = []

    def ok(name: str, detail: str = "") -> None:
        checks.append({"check": name, "result": "OK", "detail": detail})

    def bad(name: str, detail: str) -> None:
        checks.append({"check": name, "result": "FAIL", "detail": detail})
        err = SourceProofError(f"source 验证失败 · {name}：{detail}")
        err.checks = list(checks)   # 失败也要留可审计的检查链
        raise err

    if not IDENT_RE.match(req.metric):
        bad("metric_identifier", f"非法指标标识符：{req.metric!r}")

    con = _open_ro(req.ledger_db)
    try:
        # 1) query_id 必须真实存在
        row = con.execute(
            "SELECT query_id, run_id, metric, track, via_semantic "
            "FROM queries WHERE query_id = ?", (req.query_id,)
        ).fetchone()
        if row is None:
            bad("query_exists", f"query_id 未在台账登记：{req.query_id!r}（伪造来源）")
        q_id, q_run, q_metric, q_track, via_semantic = row
        ok("query_exists", f"query_id={q_id}")

        # 2) 必须经语义层取数
        if not int(via_semantic or 0):
            bad("via_semantic", "该 query 未记录为经 Semantic API 取数（via_semantic=0）")
        ok("via_semantic", "via_semantic=1")

        # 3) query 与 claim / 结论对象三对齐
        if q_run != req.run_id:
            bad("query_run_match", f"query.run_id={q_run!r} ≠ claim.run_id={req.run_id!r}")
        ok("query_run_match", f"run_id={q_run}")
        if q_track != req.track:
            bad("query_track_match", f"query.track={q_track!r} ≠ 结论 track={req.track!r}")
        ok("query_track_match", f"track={q_track}")
        if q_metric != req.metric:
            bad("query_metric_match", f"query.metric={q_metric!r} ≠ claim.metric={req.metric!r}")
        ok("query_metric_match", f"metric={q_metric}")

        # 4) run 台账的 snapshot_id 必须等于 claim 的 source
        rrow = con.execute(
            "SELECT run_id, track, snapshot_id, snapshot_sha256 FROM runs WHERE run_id = ?",
            (req.run_id,),
        ).fetchone()
        if rrow is None:
            bad("run_exists", f"run_id 未在台账登记：{req.run_id!r}")
        _, r_track, r_snap, r_sha = rrow
        if r_track != req.track:
            bad("run_track_match", f"run.track={r_track!r} ≠ 结论 track={req.track!r}")
        if r_snap != req.snapshot_id:
            bad("run_snapshot_match",
                f"run.snapshot_id={r_snap!r} ≠ claim.source.snapshot_id={req.snapshot_id!r}")
        ok("run_snapshot_match", f"snapshot_id={r_snap}")
    finally:
        con.close()

    # 5) 快照必须存在且实时哈希等于台账记录
    if not req.snapshot_file.exists():
        bad("snapshot_exists", f"快照文件不存在：{req.snapshot_file.name}")
    if not r_sha:
        bad("snapshot_sha_recorded",
            "run 台账未记录快照指纹——历史快照完整性不可确认，不得用于对账")
    actual = _sha256(req.snapshot_file)
    if actual != r_sha:
        bad("snapshot_sha_match",
            f"快照实时 sha256={actual[:12]}… ≠ 台账记录 {str(r_sha)[:12]}…（快照已被替换）")
    ok("snapshot_sha_match", f"sha256={actual[:12]}…")

    # 6) 事实源只来自该精确快照的重放
    from core.semantic.api import replay_from_snapshot  # 局部导入避免初始化期循环

    rows = replay_from_snapshot(req.snapshot_file, req.metric)
    ok("replay_from_exact_snapshot", f"{len(rows)} 个周期点")
    return {"replay": {str(dt): float(v) for dt, v in rows}, "checks": checks}


def reconcile_verified(
    claims: list[dict[str, Any]],
    req: SourceRequest,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """P0-02 唯一对账入口：先证明 source，再从精确快照重放并逐字段 diff。

    - source 无法证明 → 全部 FAIL + `quality_gate=blocked` + 审计事件（不抛异常，
      交给上游按「对账未过」拦截渲染）；
    - 对账不一致 → 同样 blocked + 审计事件。
    """
    def _blocked(reason: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
        ledger.log_event(
            req.ledger_db, "claims_blocked", subject_id=req.run_id, track=req.track,
            payload={"reason": reason, "query_id": req.query_id,
                     "snapshot_id": req.snapshot_id, "metric": req.metric,
                     "checks": checks[-6:]},
        )
        failed = [{**c, "reconcile": "FAIL", "reason": reason} for c in claims]
        return {
            "claims": failed,
            "reconcile_diff": "MISMATCH",
            "quality_gate": "blocked",
            "failed": failed,
            "source_proof": {"ok": False, "reason": reason, "checks": checks},
        }

    try:
        proof = prove_source(req)
    except SourceProofError as e:
        return _blocked(str(e), getattr(e, "checks", []) or [])
    except ClaimError as e:
        return _blocked(str(e), [])

    result = reconcile(
        claims, proof["replay"],
        unit_range=range_from_contract(contract) if contract else None,
    )
    result["source_proof"] = {"ok": True, "checks": proof["checks"]}
    if result["quality_gate"] == "blocked":
        ledger.log_event(
            req.ledger_db, "claims_reconcile_failed", subject_id=req.run_id, track=req.track,
            payload={"failed": [f.get("period") for f in result.get("failed", [])],
                     "snapshot_id": req.snapshot_id, "query_id": req.query_id},
        )
    return result


__all__ = [
    "SourceRequest", "SourceProofError", "prove_source",
    "reconcile_verified", "IDENT_RE",
]
