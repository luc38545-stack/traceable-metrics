"""论文轨断言闸门钩子（S3 论文轨 · D2「断言同行」· 与 commerce 同引擎）。

与 plugins/commerce/assertions_hook.py 的分工：断言**引擎**是共享核心
（core.quality.assertions，一份实现），本模块只做论文轨的口径适配——
dwd_cohort 的列口径是「论文轨已知口径」：

- participant_id 唯一 + 非空（blocking：研究队列主键不可重复，重复即失效）；
- arm 枚举 {a, b}（non-blocking：警告不熔断，与 commerce 枚举同策略）；
- converted 枚举 {0, 1, '0', '1'}（non-blocking：布尔/字符串双形态都算合法）；
- 行数突变带 L1（沿用默认 ±40%，历史来自本轨 run 台账）。

列不存在（DESCRIBE 探测）→ 如实不注册，不误熔断。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.quality.assertions import (
    AssertionEngine,
    default_pipeline_assertions,
)

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")

__all__ = ["recent_run_rows", "run_research_assertion_gate"]


def recent_run_rows(runs_dir: Path, limit: int = 7) -> list[int]:
    """最近 N 次成功论文轨 run 的行数（L1 突变带历史观测点）。

    与 commerce 同策略：只读 r-*.json 台账；历史不足如实返回短序列，
    引擎对空历史标注「无历史可对比」，不编造均值。
    """
    hist: list[int] = []
    for f in sorted(runs_dir.glob("r-*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 台账损坏跳过，不阻断管道
            continue
        if d.get("rows"):
            hist.append(int(d["rows"]))
        if len(hist) >= limit:
            break
    return hist


def run_research_assertion_gate(
    conn,
    runs_dir: Path,
    *,
    table: str = "dwd_cohort",
) -> dict:
    """对 dwd_cohort 跑断言闸门，返回 Report.to_dict()（可 JSON 序列化、落台账）。

    blocking 失败抛 AssertionBlocked（由管道层 try 接 → 显性失败并登记）。
    """
    if not _IDENT_RE.match(table):
        raise ValueError(f"非法表名: {table!r}")
    cols = {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}

    enum_cols: dict = {}
    if "arm" in cols:
        enum_cols["arm"] = ["a", "b"]
    if "converted" in cols:
        enum_cols["converted"] = [0, 1, "0", "1", "true", "false", "TRUE", "FALSE"]

    engine = AssertionEngine(default_pipeline_assertions(
        history=recent_run_rows(runs_dir),
        key_col="participant_id" if "participant_id" in cols else None,
        not_null_cols=[],
        enum_cols=enum_cols,
        enum_blocking=False,
        table=table,
    ))
    return engine.run(conn, {"table": table}).to_dict()
