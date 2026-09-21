"""论文轨组计数（S3 论文轨 · 插件层取数适配）。

商业轨的取数走 Semantic API；论文轨的分析输入是**分组计数**——
把 dwd_cohort 折叠成 (s_a, n_a, s_b, n_b)，喂给 core.statistics.executor.analyze。

规则：
- **标识符安全**：表名/列名过 _IDENT_RE，非法直接 ValueError（R9 同源纪律）；
- **未知 arm 显性拒绝**：出现 a/b 之外的 arm 值 → ValueError（不得静默丢弃，
  否则计数悄悄错——P4 失败显性）；
- **converted 真值口径**：'1'/'true'/'yes'/'t'（忽略大小写）计为转化，
  其余（'0'/'false'/空）计为未转化——口径文档化，不靠猜。
- 审计由管道层调用 ledger.log_query（本模块只做纯计算，core 不感知位置）。
"""
from __future__ import annotations

import re
from typing import Any

_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")
_ARMS = ("a", "b")

__all__ = ["group_counts", "GroupCountError"]


class GroupCountError(ValueError):
    """组计数输入不合法（未知 arm / 非法标识符）。"""


def _ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise GroupCountError(f"非法{what}: {name!r}（只允许字母/数字/下划线/中文）")
    return name


def group_counts(
    conn,
    table: str = "dwd_cohort",
    arm_col: str = "arm",
    converted_col: str = "converted",
) -> dict[str, Any]:
    """按 arm 分组统计转化计数。

    返回 {"a": {"converted": s_a, "total": n_a},
          "b": {"converted": s_b, "total": n_b},
          "counts": (s_a, n_a, s_b, n_b)}——counts 元组顺序与
    analyze(counts=...) 的 (s_a, n_a, s_b, n_b) 完全一致。
    """
    t = _ident(table, "表名")
    arm = _ident(arm_col, "列名")
    conv = _ident(converted_col, "列名")

    arms_present = {r[0] for r in conn.execute(
        f'SELECT DISTINCT "{arm}" FROM "{t}" WHERE "{arm}" IS NOT NULL').fetchall()}
    unknown = sorted(str(v) for v in arms_present if str(v) not in _ARMS)
    if unknown:
        raise GroupCountError(
            f"队列存在未知 arm 值 {unknown}（仅允许 {list(_ARMS)}）——"
            "组计数拒绝静默丢弃，请先修正数据"
        )

    out: dict[str, dict[str, int]] = {}
    counts: list[int] = []
    for arm_val in _ARMS:
        total = int(conn.execute(
            f'SELECT COUNT(*) FROM "{t}" WHERE "{arm}" = ?', (arm_val,)
        ).fetchone()[0])
        converted = int(conn.execute(
            f'SELECT COUNT(*) FROM "{t}" WHERE "{arm}" = ? '
            f'AND LOWER(CAST("{conv}" AS VARCHAR)) IN (\'1\',\'true\',\'yes\',\'t\')',
            (arm_val,),
        ).fetchone()[0])
        out[arm_val] = {"converted": converted, "total": total}
        counts.append(converted)
        counts.append(total)
    out["counts"] = tuple(counts)  # (s_a, n_a, s_b, n_b)
    return out
