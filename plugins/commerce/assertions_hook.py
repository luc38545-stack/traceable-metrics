"""app 层断言闸门钩子（D2「断言同行」· S1「红灯封下游」）。

分工（架构书 §02 分层）：
- ``core.quality.assertions`` 是三级断言**引擎**（无轨语义，一份实现）；
- 本模块做 app 层两件轨道适配事——① 读本轨 run 台账的行数历史（core 不感知
  数据位置，C-4）；② 按 dwd 表实际列构造默认断言集（列口径是「本轨已知口径」）。

两条管道（run_v1 CLI / webapp）共用本钩子，避免同逻辑写两份后行为漂移（P8'）。
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


def recent_row_history(runs_dir: Path, limit: int = 7) -> list[int]:
    """最近 N 次成功 run 的行数（L1 行数突变带的历史观测点）。

    只读 r-*.json 台账（不含进行中的本次）；历史不足时如实返回较短序列，
    引擎对空历史标注「无历史可对比，跳过突变检测」（不编造 7 日均值）。
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


def run_assertion_gate(
    conn,
    runs_dir: Path,
    *,
    table: str = "dwd_base",
    key_col: str = "order_id",
    not_null_cols: tuple[str, ...] = (),
    enum_cols: dict | None = None,
) -> dict:
    """对 dwd 表跑断言闸门，返回 Report.to_dict()（可 JSON 序列化、落台账）。

    - 只注册**真实存在**的列（DESCRIBE 探测）——缺列如实不注册，不误熔断；
    - 默认红线 = 行数突变带 ±40%（blocking）+ 主键唯一/非空（blocking）；
      金额列空值由体检引擎（warn，可修复）负责提示，**不**在断言闸门熔断——
      否则上传含空值的真实 CSV 会直接走不完主旅程（D12 零代码主旅程破坏）；
    - 枚举断言默认 non-blocking（记录警告），避免把真实业务枚举误当脏数据熔断；
    - blocking 失败抛 ``AssertionBlocked``，由调用方 try 接 → 管道显性失败并登记
      （P4 红灯封下游，S1 出口判据「注入脏数成功封堵」）。
    """
    if not _IDENT_RE.match(table):
        raise ValueError(f"非法表名: {table!r}")
    if enum_cols is None:
        enum_cols = {"status": ["paid", "cancelled"]}
    cols = {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}
    engine = AssertionEngine(default_pipeline_assertions(
        history=recent_row_history(runs_dir),
        key_col=key_col if key_col in cols else None,
        not_null_cols=[c for c in not_null_cols if c in cols],
        enum_cols={c: v for c, v in enum_cols.items() if c in cols},
    ))
    return engine.run(conn, {"table": table}).to_dict()


__all__ = ["recent_row_history", "run_assertion_gate"]
