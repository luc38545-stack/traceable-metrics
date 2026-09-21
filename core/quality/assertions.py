"""三级断言引擎（共享核心 · 架构书 §08.1「断言体系三级一份实现」+ §07 D2「断言同行」+ §10 S1「红灯封下游」）。

对应《施工模型对比赛章程·冻结》§二 考题规格（唯一依据）：
- ``Assertion(name, level, fn, human_text)`` + ``AssertionEngine.run(table, conn) → Report``
- level ∈ {L1摄取, L2资产, L3指标}，blocking 属性必填
- L1 行数突变带（对 7 日均值 ±40% 熔断）；L2 唯一性/非空/枚举字典/引用完整；
  L3 跨源对账、环比合理性界（示例实现 ≥1 个即可，覆盖类型即可）
- blocking 失败 → ``Report.blocked=True``，引擎抛 ``AssertionBlocked``（下游用 try 接）；
  non-blocking 失败 → 记录不抛
- ``Report.to_dict()`` 可 JSON 序列化：每条断言 {name, level, blocking, passed,
  human_text, affected_rows}

硬约束（对比赛章程 §三，与 R1-R9 门禁一致）：
- C-1：本模块不 import plugins/*；
- C-4：本模块不出现任何 /data/* 路径字面量——表名/列名全部由调用方传入并校验；
- C-5 精神：断言结果只出 Report 对象，不在引擎内写任何报告/对话产物。

V1 边界（诚实标注，非伪装）：
- 断言的**声明式清单**（YAML「模型 PR 必附断言」的持久化形态，P2 声明式优先）
  尚未建立；V1 以「代码内注册 + 可 JSON 序列化的 Report」落地，声明式清单进
  backlog（见审计报告第八节争议标注）。
- L1 行数突变带严格语义需要 ≥7 个历史观测点；起步阶段历史不足时如实标注
  「无历史可对比，跳过突变检测」，不编造 7 日均值。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

# 对比赛章程 level ∈ {L1摄取, L2资产, L3指标} 的机器可读形态（文档保留中文全称）
LEVELS = ("L1", "L2", "L3")
LEVEL_LABEL = {"L1": "L1摄取", "L2": "L2资产", "L3": "L3指标"}

# 标识符守卫：表名/列名只允许字母数字下划线中文（防 SQL 注入；中文字段如「订单编号」）
_IDENT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")


def _ident(name: str, what: str) -> str:
    """构造期校验标识符，非法立即拒绝（不等到运行时拼出 SQL 才炸）。"""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"非法{what}: {name!r}（只允许字母/数字/下划线/中文）")
    return name


def _lit(v: Any) -> str:
    """SQL 字面量转义：字符串单引号翻倍；数字原样；其余拒绝。"""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise ValueError(f"断言枚举字典只支持 str/int/float/bool，收到 {type(v).__name__}")


@dataclass
class Assertion:
    """一条可独立验收的断言。

    - level：L1摄取 / L2资产 / L3指标（对比赛章程三值）
    - blocking：必填——True=失败即熔断下游；False=失败仅记录
    - fn：``(conn, ctx) -> (passed: bool, affected_rows: int, detail: str)``，
      conn 为存储连接（测试/管道注入），ctx 携带 table 等上下文。
    """

    name: str
    level: str
    blocking: bool
    human_text: str
    fn: Callable[[Any, dict], tuple[bool, int, str]]

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"非法 level: {self.level!r}（只能是 {LEVELS}）")
        if not isinstance(self.blocking, bool):
            raise TypeError(f"blocking 必填且必须为 bool（对比赛章程 §二）：{self.name}")


@dataclass
class AssertionReport:
    """断言结果容器（可 JSON 序列化）。"""

    results: list[dict] = field(default_factory=list)
    blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"blocked": self.blocked, "assertions": list(self.results)}


class AssertionBlocked(RuntimeError):
    """blocking 断言失败：引擎熔断，携带完整 Report 供下游出诊断。

    对比赛章程 §二：blocking 失败 → Report.blocked=True，引擎抛 AssertionBlocked
    （下游用 try 接，**不许静默放行**——P4 失败显性化）。
    """

    def __init__(self, report: AssertionReport) -> None:
        self.report = report
        failed = [
            f"{r['name']}（{r['human_text']}，影响 {r['affected_rows']} 行）"
            for r in report.results if not r["passed"] and r["blocking"]
        ]
        # 前缀由管道层统一加（raise ModelingError(f"断言熔断（红灯封下游）：{e}")），
        # 这里只报失败清单，避免文案重复。
        super().__init__("；".join(failed))


class AssertionEngine:
    """按注册顺序执行断言；任何 blocking 失败 → blocked=True 并抛 AssertionBlocked。"""

    def __init__(self, assertions: Sequence[Assertion] | None = None) -> None:
        self._assertions: list[Assertion] = list(assertions or [])

    def register(self, a: Assertion) -> None:
        self._assertions.append(a)

    def run(self, conn: Any, ctx: dict | None = None) -> AssertionReport:
        """执行全部断言。

        - blocking 失败：记入 Report 并标记 blocked=True，全部跑完后抛 AssertionBlocked
          （带上 Report，调用方 try 接后既可看失败单，也可决定是否熔断管道）；
        - non-blocking 失败：只记录，不抛（对比赛章程 §二）。
        - 断言执行本身抛异常 → 视为失败（blocking 按配置生效），不静默吞。
        """
        ctx = ctx or {}
        report = AssertionReport()
        for a in self._assertions:
            try:
                passed, affected, detail = a.fn(conn, ctx)
            except Exception as e:  # noqa: BLE001 — 断言执行异常也按失败处置
                passed, affected, detail = False, 0, f"断言执行异常: {e}"
            entry = {
                "name": a.name,
                "level": a.level,
                "blocking": a.blocking,
                "passed": bool(passed),
                "human_text": a.human_text,
                "affected_rows": int(affected or 0),
                "detail": detail,
            }
            report.results.append(entry)
            if not passed and a.blocking:
                report.blocked = True
        if report.blocked:
            raise AssertionBlocked(report)
        return report


# ================= 内建断言（三级，均为工厂函数） =================


def row_count_band(
    history: Sequence[int] | None = None,
    band: float = 0.40,
    name: str = "L1_行数突变带",
    blocking: bool = True,
) -> Assertion:
    """L1 摄取 · 行数突变带：对 7 日均值 ±40% 熔断。

    - history：最近行数观测点（架构书 7 日均值；调用方从 run 历史/台账注入）。
      为空 → 如实标注「无历史可对比，跳过突变检测」，不编造均值。
    - band：带宽度（默认 0.40 = ±40%，架构书 S1 熔断语义）。
    - 当前行数 = ctx["table"] 的 COUNT(*)。
    """
    if not (0 < band < 1):
        raise ValueError(f"band 必须在 (0,1)：{band!r}")

    def fn(conn: Any, ctx: dict) -> tuple[bool, int, str]:
        table = _ident(str(ctx.get("table", "dwd_base")), "表名")
        current = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        hist = [int(h) for h in (history or [])][-7:]     # 取最近至多 7 个点
        if not hist:
            return True, 0, "无历史行数可对比，跳过突变检测（如实标注，非静默）"
        mean = sum(hist) / len(hist)
        lo, hi = mean * (1 - band), mean * (1 + band)
        if lo <= current <= hi:
            return (
                True, 0,
                f"当前 {current} 行在带内（历史均值 {mean:.0f} ±{band:.0%}，"
                f"基于 {len(hist)} 个观测点）",
            )
        delta = (current - mean) / mean
        return (
            False, current,
            f"行数突变 {delta:+.0%}（当前 {current} vs 均值 {mean:.0f}，"
            f"带 ±{band:.0%}，基于 {len(hist)} 个观测点）",
        )

    return Assertion(
        name, "L1", blocking, f"行数突变带（7 日均值 ±{band:.0%} 熔断）", fn
    )


def not_null(col: str, table: str = "dwd_base", blocking: bool = True) -> Assertion:
    """L2 资产 · 非空：空串/纯空白/NULL 都算空值。"""
    col, table = _ident(col, "列名"), _ident(table, "表名")

    def fn(conn: Any, ctx: dict) -> tuple[bool, int, str]:
        t = _ident(str(ctx.get("table", table)), "表名")
        n = int(conn.execute(
            f'SELECT COUNT(*) FROM "{t}" '
            f'WHERE "{col}" IS NULL OR TRIM(CAST("{col}" AS VARCHAR)) = \'\''
        ).fetchone()[0])
        if n == 0:
            return True, 0, f"「{col}」列无空值"
        return False, n, f"「{col}」列有 {n} 行空值"

    return Assertion(f"L2_非空_{col}", "L2", blocking, f"「{col}」不允许空值", fn)


def unique(col: str, table: str = "dwd_base", blocking: bool = True) -> Assertion:
    """L2 资产 · 唯一性：列内重复组的行数 >0 即失败。"""
    col, table = _ident(col, "列名"), _ident(table, "表名")

    def fn(conn: Any, ctx: dict) -> tuple[bool, int, str]:
        t = _ident(str(ctx.get("table", table)), "表名")
        n_groups = int(conn.execute(
            f'SELECT COUNT(*) FROM (SELECT "{col}" FROM "{t}" '
            f'GROUP BY "{col}" HAVING COUNT(*) > 1)'
        ).fetchone()[0])
        n_rows = int(conn.execute(
            f'SELECT COALESCE(SUM(cnt),0) FROM ('
            f'SELECT COUNT(*) cnt FROM "{t}" GROUP BY "{col}" HAVING COUNT(*) > 1)'
        ).fetchone()[0])
        if n_groups == 0:
            return True, 0, f"「{col}」列无重复"
        return False, n_rows, f"「{col}」列有 {n_groups} 组重复（共 {n_rows} 行）"

    return Assertion(f"L2_唯一_{col}", "L2", blocking, f"「{col}」必须唯一", fn)


def enum_values(
    col: str,
    allowed: Sequence[str | int | float | bool],
    table: str = "dwd_base",
    blocking: bool = True,
) -> Assertion:
    """L2 资产 · 枚举字典：值不在字典内的行数 >0 即失败。

    类型健壮（2026-08-31 修正）：列值一律转小写字符串再比——整数列（如
    converted 0/1）与 'true'/'1' 等字符串字面量混比时，duckdb 的隐式类型
    强转会抛 Conversion Error（实测复现：INT 列 NOT IN (0,1,'true')）。
    字符串形态比较对 int/bool/str 枚举都成立，且不改变任一现有语义。
    """
    col, table = _ident(col, "列名"), _ident(table, "表名")
    allowed_set = [str(v).lower() for v in allowed]  # 统一小写字符串形态

    def fn(conn: Any, ctx: dict) -> tuple[bool, int, str]:
        t = _ident(str(ctx.get("table", table)), "表名")
        if not allowed_set:
            return True, 0, "枚举字典为空，跳过（如实标注）"
        allow_sql = ", ".join(_lit(v) for v in allowed_set)
        n = int(conn.execute(
            f'SELECT COUNT(*) FROM "{t}" '
            f'WHERE "{col}" IS NOT NULL '
            f'AND LOWER(CAST("{col}" AS VARCHAR)) NOT IN ({allow_sql})'
        ).fetchone()[0])
        if n == 0:
            return True, 0, f"「{col}」列值全部在枚举字典内"
        return False, n, f"「{col}」列有 {n} 行不在枚举字典 {allowed_set} 内"

    return Assertion(f"L2_枚举_{col}", "L2", blocking, f"「{col}」值必须在枚举字典内", fn)


def referential_integrity(
    child_col: str,
    parent_table: str,
    parent_col: str,
    table: str = "dwd_base",
    blocking: bool = True,
) -> Assertion:
    """L2 资产 · 引用完整：子表外键指向父表不存在的主键 → 孤儿行。"""
    child_col = _ident(child_col, "列名")
    parent_table, parent_col = _ident(parent_table, "表名"), _ident(parent_col, "列名")
    table = _ident(table, "表名")

    def fn(conn: Any, ctx: dict) -> tuple[bool, int, str]:
        t = _ident(str(ctx.get("table", table)), "表名")
        n = int(conn.execute(
            f'SELECT COUNT(*) FROM "{t}" c '
            f'LEFT JOIN "{parent_table}" p ON c."{child_col}" = p."{parent_col}" '
            f'WHERE c."{child_col}" IS NOT NULL AND p."{parent_col}" IS NULL'
        ).fetchone()[0])
        if n == 0:
            return True, 0, f"「{t}.{child_col}」无孤儿行（父表 {parent_table}.{parent_col}）"
        return False, n, f"「{t}.{child_col}」有 {n} 行孤儿（父表 {parent_table} 无对应 {parent_col}）"

    return Assertion(
        f"L2_引用_{child_col}", "L2", blocking,
        f"「{child_col}」必须引用存在的 {parent_table}.{parent_col}", fn,
    )


def cross_source_reconcile(
    source_table: str,
    source_col: str,
    target_table: str,
    target_col: str,
    key_col: str = "order_id",
    tolerance: float = 1e-9,
    blocking: bool = True,
) -> Assertion:
    """L3 指标 · 跨源对账：同 key 下两表数值列逐行 diff 超容差的行数。

    （架构书 §08.1「L3 … 跨源对账」示例实现；容差默认 1e-9，与 claims 对账口径一致。）
    """
    source_table, source_col = _ident(source_table, "表名"), _ident(source_col, "列名")
    target_table, target_col = _ident(target_table, "表名"), _ident(target_col, "列名")
    key_col = _ident(key_col, "列名")

    def fn(conn: Any, ctx: dict) -> tuple[bool, int, str]:
        n = int(conn.execute(
            f'SELECT COUNT(*) FROM "{source_table}" s '
            f'JOIN "{target_table}" t ON s."{key_col}" = t."{key_col}" '
            f'WHERE ABS(CAST(s."{source_col}" AS DOUBLE) '
            f'- CAST(t."{target_col}" AS DOUBLE)) > {tolerance}'
        ).fetchone()[0])
        if n == 0:
            return True, 0, f"{source_table}.{source_col} 与 {target_table}.{target_col} 跨源一致"
        return False, n, (
            f"{source_table}.{source_col} 与 {target_table}.{target_col} "
            f"有 {n} 行不一致（容差 {tolerance}）"
        )

    return Assertion(
        f"L3_跨源_{source_col}", "L3", blocking,
        f"「{source_col}」与 {target_table}.{target_col} 必须跨源一致", fn,
    )


def trend_reasonableness(
    metric_name: str,
    current: float,
    previous: float | None,
    bound: float,
    blocking: bool = False,
) -> Assertion:
    """L3 指标 · 环比合理性界（示例实现，默认 non-blocking 警告）。

    变化率 = (current - previous) / |previous|；超过 bound → 失败（记录不熔断）。
    previous 为 None 或 0 → 无前期值，跳过并如实标注。
    """
    if not (0 < bound < 10):
        raise ValueError(f"bound 必须在 (0,10)：{bound!r}")

    def fn(conn: Any, ctx: dict) -> tuple[bool, int, str]:
        if previous is None or previous == 0:
            return True, 0, "无前期值，跳过环比合理性检查（如实标注）"
        delta = (current - previous) / abs(previous)
        if abs(delta) <= bound:
            return True, 0, f"{metric_name} 环比 {delta:+.1%}，在界 ±{bound:.0%} 内"
        return False, 0, f"{metric_name} 环比 {delta:+.1%}，超界 ±{bound:.0%}"

    return Assertion(
        f"L3_环比_{metric_name}", "L3", blocking,
        f"「{metric_name}」环比变化不得超界 ±{bound:.0%}", fn,
    )


def default_pipeline_assertions(
    history: Sequence[int] | None = None,
    *,
    band: float = 0.40,
    key_col: str | None = None,
    not_null_cols: Sequence[str] = (),
    enum_cols: dict[str, Sequence[str | int | float | bool]] | None = None,
    enum_blocking: bool = False,
    table: str = "dwd_base",
) -> list[Assertion]:
    """管道默认断言集（D2「断言同行」的最小落地，供 app 层两条管道共用）。

    - history：L1 行数突变带的历史观测点（调用方从 run 台账注入；core 不感知数据位置）；
    - key_col：主键列 → 唯一性 + 非空（如 order_id）；
    - not_null_cols：关键列非空（如金额列）；
    - enum_cols：{列: 允许值}，默认 non-blocking（记录警告，不误熔断真实业务枚举）；
    - table：目标表（默认 dwd_base）。

    **调用方责任**：列口径是「本轨已知口径」，由 app 层探测列存在性后
    决定传哪些列——core 不感知任何列口径，也不感知数据位置（C-4）。
    """
    out: list[Assertion] = [row_count_band(history=history, band=band)]
    if key_col:
        out.append(unique(key_col, table=table))
        out.append(not_null(key_col, table=table))
    for c in not_null_cols:
        out.append(not_null(c, table=table))
    for c, allowed in (enum_cols or {}).items():
        out.append(enum_values(c, allowed, table=table, blocking=enum_blocking))
    return out


__all__ = [
    "Assertion", "AssertionEngine", "AssertionReport", "AssertionBlocked",
    "row_count_band", "not_null", "unique", "enum_values",
    "referential_integrity", "cross_source_reconcile", "trend_reasonableness",
    "default_pipeline_assertions",
]
