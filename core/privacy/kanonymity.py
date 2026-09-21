"""k-匿名检查（S3 论文轨 · 架构书 §六「k 匿名」 · P1-06 语义安全同源）。

论文轨产出可发表数据前的隐私闸门：按准标识符（quasi-identifiers）分组，
任一组的行数 < k 意味着该组合可唯一定位到个体 → 拒绝（或先泛化再重查）。

规则：
1. **标识符安全。** 表名与准标识符列名必须通过 _ident 校验（^[A-Za-z_][A-Za-z0-9_]*$），
   任何拼接形态的列名（含空格/分号等）→ ValueError，绝不放行进 SQL（R9 同源纪律）。
2. **诚实报告。** check_k_anonymity 只报告不阻断（satisfied / min_group_size /
   offending_groups / affected_rows）；assert_k_anonymity 在不满足时抛
   PrivacyViolation——闸门用后者，报告用前者。
3. **核心不选存储。** conn（duckdb 连接）由调用方注入，本模块不 import duckdb（R5）、
   不硬编码任何数据路径（R3/C-4）。
"""
from __future__ import annotations

import re
from typing import Any, Sequence

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

__all__ = ["PrivacyViolation", "check_k_anonymity", "assert_k_anonymity"]


class PrivacyViolation(RuntimeError):
    """未达 k-匿名：数据存在可唯一识别个体的分组。"""


def _ident(name: str) -> str:
    """标识符安全校验：非法 → ValueError（不放行任何注入形态）。"""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"非法标识符：{name!r}（仅允许字母/数字/下划线，且不以数字开头）")
    return name


def check_k_anonymity(
    conn, table: str, quasi_cols: Sequence[str], k: int
) -> dict[str, Any]:
    """检查表是否满足 k-匿名。返回报告（不抛异常，供展示/泛化后重查）。

    conn: duckdb 连接（调用方注入）；table/quasi_cols 均做标识符校验。
    """
    if not isinstance(k, int) or k < 1:
        raise ValueError(f"k 必须为正整数，实为 {k!r}")
    t = _ident(table)
    if not quasi_cols:
        raise ValueError("准标识符列表为空——没有准标识符则无从谈起 k-匿名")
    cols = [_ident(c) for c in quasi_cols]
    group_expr = ", ".join(f'"{c}"' for c in cols)

    rel = conn.execute(
        f'SELECT {group_expr}, COUNT(*) AS _grp_n FROM "{t}" '
        f"GROUP BY {group_expr} ORDER BY _grp_n"
    )
    rows = rel.fetchall()

    groups: list[dict[str, Any]] = []
    affected = 0
    for row in rows:
        *values, n = row
        groups.append({"group": list(values), "size": int(n)})
        if int(n) < k:
            affected += int(n)

    min_size = min((g["size"] for g in groups), default=0)
    offending = [g for g in groups if g["size"] < k]
    return {
        "satisfied": bool(offending) is False,
        "k": k,
        "min_group_size": min_size,
        "offending_groups": offending,
        "affected_rows": affected,
        "quasi_identifiers": cols,
    }


def assert_k_anonymity(conn, table: str, quasi_cols: Sequence[str], k: int) -> dict[str, Any]:
    """k-匿名闸门：不满足 → PrivacyViolation（错误信息点名违规组与规模）。"""
    rep = check_k_anonymity(conn, table, quasi_cols, k)
    if not rep["satisfied"]:
        detail = "；".join(
            f"{'/'.join(str(v) for v in g['group'])} (size={g['size']})"
            for g in rep["offending_groups"]
        )
        raise PrivacyViolation(
            f"数据未达 k={k} 匿名：{len(rep['offending_groups'])} 个分组可唯一识别个体"
            f"（{detail}），受影响 {rep['affected_rows']} 行。"
            "请先泛化准标识符或补充样本，再重新检查。"
        )
    return rep
