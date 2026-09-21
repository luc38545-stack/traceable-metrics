"""Semantic API（附录 T 冻结项③）—— 全平台唯一取数端点。

架构书要求：
- 唯一取数端点 + 查询日志表（每次取数经 ledger.log_query 落审计）
- 返回体必带 freshness / metric_version
- 负向：绕过语义层直连 DuckDB 的连接尝试 → 拒绝且审计（本模块是 core 内
  唯一允许执行 SELECT 取数的入口；插件层一律经 query_metric 取数）

数据位置由调用方注入的连接决定；core 不硬编码路径（C-4）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.audit import ledger
from core.semantic.compiler import (
    ContractError,
    assert_compiled,
    assert_identifier,
    load_contract,
)


class SemanticQueryError(ValueError):
    """取数被拒。"""


def query_metric(
    conn,
    ledger_db: Path,
    contract_path: Path,
    track: str,
    run_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """唯一取数端点：编译契约 → 查视图 → 写查询日志 → 返回带 freshness 的结果。

    参数 conn 由 app 层注入（指向本轨库文件）；本函数不选择数据位置。
    """
    contract = load_contract(contract_path)  # grain/time_column 缺失在此抛 ContractError
    metric = contract["metric"]
    view = metric

    # P1-06 ⑤：取数端只接受**已编译登记**的 (track, metric)——调用方不能凭任意
    # 契约文件就取数，更不能指定任意 view/SQL；未登记一律拒绝（统一 SemanticQueryError）。
    try:
        assert_compiled(track, metric)
    except ContractError as e:
        raise SemanticQueryError(str(e)) from e

    # 返回体必带 freshness（契约声明的最大延迟）与 metric_version
    freshness = (contract.get("freshness_contract") or {}).get("max_delay", "unknown")

    sql = f"SELECT dt, value FROM {view} ORDER BY dt"
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        rows = conn.execute(sql).fetchall()
    except Exception as e:  # noqa: BLE001
        raise SemanticQueryError(f"query blocked for metric {metric!r}: {e}") from e

    query_id = f"q-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    ledger.log_query(
        ledger_db, query_id=query_id, metric=metric, track=track,
        run_id=run_id, via_semantic=True,
    )

    return {
        "query_id": query_id,
        "metric": metric,
        "metric_version": contract.get("version", 1),
        "freshness": freshness,
        "rows": [[dt, value] for dt, value in rows],
    }


def replay_from_snapshot(snapshot_db: Path, metric: str) -> list[tuple[str, float]]:
    """ADR-18 对账基准：只读打开快照库重放取数（不写日志——重放不是新查询）。"""
    from core.storage import db as storage

    assert_identifier(metric, "metric 名")   # P1-06：拼进 SQL 前必须过标识符校验
    con = storage.connect(snapshot_db, read_only=True)
    try:
        return con.execute(f"SELECT dt, value FROM {metric} ORDER BY dt").fetchall()
    finally:
        con.close()
