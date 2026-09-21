"""语义编译器（共享核心）—— Semantic Layer 的 V1 骨架。

指标契约 YAML → 合法性校验 → DuckDB 视图。
C.2 冻结规则：grain / time_column 缺失即编译失败（CI 负面用例之一）。

V1.1：支持嵌套指标——ratio 的 numerator/denominator 可引用另一份契约
（架构书清单 4-A 形态：pay_success_rate ← pay_success_orders / pay_attempted_orders），
子契约先编译为视图，父视图按 dt 拼接。

P1-06（SQL 注入面收口）：
- 契约走严格 schema：未知字段一律拒绝（契约里的每个键都得有人负责）；
- metric / time_column / dimension / filter.field 必须匹配严格标识符规则；
- filters 从「任意 SQL 字符串」改成结构化 AST `{field, op, value}`：
  字段走标识符规则、操作符走 allowlist、值一律参数化（不拼进 SQL 文本）；
- 编译成功的 (track, metric) 进已登记集合，取数端只认已登记指标。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml


REQUIRED_FIELDS = ("metric", "type", "grain", "time_column")
RATIO_FIELDS = ("numerator", "denominator")
ALLOWED_TYPES = ("ratio", "sum", "count", "avg")

# P1-06：严格标识符规则（不含引号、不含点号、不含分号、不以数字开头）
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# 操作符 allowlist：过滤器里除了这些，什么都不认
ALLOWED_OPS = {
    "=", "!=", "<>", "<", "<=", ">", ">=",
    "IN", "NOT IN", "IS NULL", "IS NOT NULL",
}
NULL_OPS = {"IS NULL", "IS NOT NULL"}
MULTI_OPS = {"IN", "NOT IN"}

# 契约允许的字段白名单（严格 schema：出现未知键即拒绝，避免「写了但没生效」）
ALLOWED_CONTRACT_FIELDS = {
    "metric", "description", "type", "grain", "time_column",
    "numerator", "denominator", "filters", "queryable_dimensions",
    "freshness_contract", "owners", "version", "value_range",
    # P2-04 场景 2/3：count 契约可声明 order_key，按订单去重（count DISTINCT）
    "order_key",
}

# 已编译指标登记表（进程内）：取数端只认这里登记过的 (track, metric)
_COMPILED: set[tuple[str, str]] = set()


class ContractError(ValueError):
    """指标契约不合法。"""


def assert_identifier(value: Any, what: str) -> str:
    """P1-06：任何拼进 SQL 的名字都必须先过这道规则。"""
    if not isinstance(value, str) or not IDENT_RE.match(value):
        raise ContractError(
            f"非法{what}：{value!r}（只允许 [A-Za-z_][A-Za-z0-9_]{{0,62}}，"
            f"不得含引号/点号/分号/空格）"
        )
    return value


def register_compiled(track: str, metric: str) -> None:
    _COMPILED.add((track, assert_identifier(metric, "metric 名")))


def assert_compiled(track: str, metric: str) -> None:
    """取数端守卫：未在本进程编译登记的指标不得取数。"""
    assert_identifier(metric, "metric 名")
    if (track, metric) not in _COMPILED:
        raise ContractError(
            f"指标 {metric!r}（track={track!r}）未编译登记——"
            f"取数端只接受已登记 metric ID，不接受调用方任意指定的视图或 SQL"
        )


def _validate_contract_schema(raw: dict[str, Any], name: str) -> None:
    """严格 schema：必填、类型、未知键三重校验。"""
    for f in REQUIRED_FIELDS:
        if f not in raw:
            raise ContractError(f"missing required field: {f} (in {name})")
    unknown = set(raw) - ALLOWED_CONTRACT_FIELDS
    if unknown:
        raise ContractError(
            f"contract has unknown fields {sorted(unknown)} (in {name})——"
            f"严格 schema：契约里不允许出现没人负责的键"
        )
    if raw["type"] not in ALLOWED_TYPES:
        raise ContractError(f"metric.type must be one of {ALLOWED_TYPES}, got {raw['type']!r}")
    assert_identifier(raw["metric"], "metric 名")
    assert_identifier(raw["time_column"], "time_column")
    if not isinstance(raw["grain"], str) or not raw["grain"].strip():
        raise ContractError(f"grain 必须是非空字符串 (in {name})")
    if raw["type"] == "ratio":
        for f in RATIO_FIELDS:
            if f not in raw:
                raise ContractError(f"ratio contract missing required field: {f} (in {name})")
    # P2-04 场景 2/3：order_key 只能出现在 count 契约上（去重才有意义），
    # 且必须过标识符规则（P1-06 拼进 SQL 前校验）。
    order_key = raw.get("order_key")
    if order_key is not None:
        if raw["type"] != "count":
            raise ContractError(
                f"order_key 只允许出现在 count 契约上（in {name}），type={raw['type']!r}"
            )
        assert_identifier(order_key, "order_key")
    for f in RATIO_FIELDS if raw["type"] == "ratio" else ():
        sub = raw[f]
        if not isinstance(sub, dict) or not sub.get("metric"):
            raise ContractError(f"{f} 必须是含 metric 的映射 (in {name})")
        assert_identifier(sub["metric"], f"{f}.metric")
    if raw["type"] in ("sum", "avg"):
        sub = raw.get("numerator")
        if not isinstance(sub, dict) or not sub.get("metric"):
            raise ContractError(f"{raw['type']} 契约必须给出 numerator.metric (in {name})")
        assert_identifier(sub["metric"], "numerator.metric")
    for d in raw.get("queryable_dimensions") or []:
        assert_identifier(d, "queryable_dimensions 项")
    rng = raw.get("value_range")
    if rng is not None:
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            raise ContractError(f"value_range 必须是 [min, max] (in {name})")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in rng):
            raise ContractError(f"value_range 的两项必须是数字 (in {name})")
    ver = raw.get("version", 1)
    if not isinstance(ver, int) or isinstance(ver, bool):
        raise ContractError(f"version 必须是整数 (in {name})")


def _sql_literal(v: Any, name: str, idx: int) -> str:
    """P1-06：过滤器值 → 类型化 SQL 字面量（绝不把用户文本原样拼进 SQL）。

    DuckDB 的 CREATE VIEW 是 DDL，不支持 `?` 预处理参数，因此这里用
    「严格类型 + 程序化转义」实现同等安全：字符串单引号加倍、数字走
    repr、bool/None 走关键字——注入值只能作为一个整体字面量存在。
    """
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if not math.isfinite(float(v)):
            raise ContractError(f"filters[{idx}] 的值必须有限（NaN/Inf 拒绝）(in {name})")
        return repr(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise ContractError(
        f"filters[{idx}] 的值类型不支持：{type(v).__name__} "
        f"(只允许 str/int/float/bool/None) (in {name})"
    )


def build_where(filters: Any, name: str) -> str:
    """P1-06：结构化过滤器 → 注入安全的 WHERE 子句。

    入参形如 `[{"field": "status", "op": "=", "value": "paid"}]`。
    - field 走标识符规则；op 走 allowlist；value 经类型化转义（`_sql_literal`）；
    - 旧格式的裸 SQL 字符串（如 `"status = 'paid'"`）直接拒绝，
      不给「看起来还能跑」的兼容通道——那正是注入面。
    """
    if filters is None:
        return "1=1"
    if not isinstance(filters, list):
        raise ContractError(f"filters 必须是列表 (in {name})")
    clauses: list[str] = []
    for i, f in enumerate(filters):
        if isinstance(f, str):
            raise ContractError(
                f"filters[{i}] 是裸 SQL 字符串 {f!r} (in {name})——"
                f"必须改成结构化形式 {{field, op, value}}（P1-06）"
            )
        if not isinstance(f, dict):
            raise ContractError(f"filters[{i}] 必须是 {{field, op, value}} 映射 (in {name})")
        unknown = set(f) - {"field", "op", "value"}
        if unknown:
            raise ContractError(f"filters[{i}] 含未知键 {sorted(unknown)} (in {name})")
        field = assert_identifier(f.get("field"), f"filters[{i}].field")
        op = f.get("op")
        if not isinstance(op, str) or op.upper() not in ALLOWED_OPS:
            raise ContractError(
                f"filters[{i}].op={op!r} 不在允许列表内：{sorted(ALLOWED_OPS)} (in {name})"
            )
        op = op.upper()
        if op in NULL_OPS:
            if "value" in f:
                raise ContractError(f"filters[{i}]：{op} 不接受 value (in {name})")
            clauses.append(f"{field} {op}")
        elif op in MULTI_OPS:
            vals = f.get("value")
            if not isinstance(vals, (list, tuple)) or not vals:
                raise ContractError(f"filters[{i}]：{op} 的 value 必须是非空列表 (in {name})")
            literals = ", ".join(_sql_literal(x, name, i) for x in vals)
            clauses.append(f"{field} {op} ({literals})")
        else:
            if "value" not in f:
                raise ContractError(f"filters[{i}]：{op} 需要 value (in {name})")
            clauses.append(f"{field} {op} {_sql_literal(f['value'], name, i)}")
    return " AND ".join(clauses) if clauses else "1=1"


def load_contract(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ContractError(f"contract file must be a mapping: {path}")
    _validate_contract_schema(raw, path.name)
    return raw


def _dwd_columns(conn) -> set[str]:
    """P1-06 ③：dwd_base 已登记模型 schema 的列集合（filter 字段必须来自这里）。"""
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'dwd_base'"
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001 — 表尚不存在时留给后续 execute 自然报错
        return set()


def _sub_contract_exists(metric_name: str, contracts_dir: Path | None) -> bool:
    """numerator/denominator 引用的名字是否为另一份契约文件。"""
    return bool(contracts_dir and (contracts_dir / f"{metric_name}.yml").exists())


def _compile_sub(
    conn, ctx_track: str, metric_name: str, contracts_dir: Path | None, seen: set[str]
) -> str | None:
    """编译子契约（若存在），返回可引用的子查询；否则 None（回退列引用）。"""
    if not _sub_contract_exists(metric_name, contracts_dir):
        return None
    assert_identifier(metric_name, "子契约 metric 名")   # P1-06：拼进 SQL 前必须过标识符
    sub = load_contract(contracts_dir / f"{metric_name}.yml")
    compile_metric(conn, ctx_track, sub, contracts_dir, seen=seen)
    return f"(SELECT dt, value FROM {metric_name})"


def compile_metric(
    conn,
    ctx_track: str,
    contract: dict[str, Any],
    contracts_dir: Path | None = None,
    seen: set[str] | None = None,
) -> str:
    """把契约编译为 DuckDB 视图。返回视图名。

    V1 版实现 ratio/sum/count/avg 四型；ratio 支持嵌套指标引用（子契约视图
    按 dt 拼接），无子契约时回退到 dwd 表基础事实列（V1 兼容）。
    视图名 = metric 名；轨道归属由 ctx_track 决定的连接决定（core 不选择数据位置）。

    P1-06：编译入口统一过严格 schema + 标识符校验 + 结构化 filter 参数化；
    编译成功的 (track, metric) 登记进 `_COMPILED`，取数端只认已登记指标。
    """
    seen = seen or set()
    name = contract["metric"]
    if name in seen:
        raise ContractError(f"circular metric reference: {name}")
    seen = seen | {name}
    # P1-06：直接调用方（不经 load_contract）也必须过严格 schema（含标识符校验）
    _validate_contract_schema(contract, f"compile:{name}")
    time_col = contract["time_column"]
    order_key = contract.get("order_key")

    # P1-06：结构化 filter → 注入安全的 WHERE（值类型化转义）；裸 SQL 字符串被拒绝
    where = build_where(contract.get("filters"), name)
    # P1-06 ③：filter 字段必须来自已登记模型 schema（未知列编译期拒绝）
    schema_cols = _dwd_columns(conn)
    for f in contract.get("filters") or []:
        if isinstance(f, dict) and f.get("field") not in schema_cols:
            raise ContractError(
                f"filters.field={f.get('field')!r} 不在 dwd_base schema 中 (in {name})："
                f"可用列 {sorted(schema_cols)}"
            )
    # P2-04 场景 2/3：order_key 必须真实存在于 dwd schema（未知列编译期拒绝，
    # 避免「写了去重键但列不存在」时 count DISTINCT 静默吞掉错误）。
    if order_key is not None and order_key not in schema_cols:
        raise ContractError(
            f"order_key={order_key!r} 不在 dwd_base schema 中 (in {name})："
            f"可用列 {sorted(schema_cols)}"
        )
    # 架构书清单 4-A：dwd 含 test_order 列时强制排除测试单（P2-04 场景 7），
    # 该过滤对父 ratio 与消费端一律不可绕过——它进的是编译后的视图本体。
    if "test_order" in schema_cols:
        where = ("test_order = false" if where == "1=1"
                 else f"({where}) AND test_order = false")

    if contract["type"] == "ratio":
        num, den = contract["numerator"]["metric"], contract["denominator"]["metric"]
        num_src = _compile_sub(conn, ctx_track, num, contracts_dir, seen)
        den_src = _compile_sub(conn, ctx_track, den, contracts_dir, seen)
        if num_src and den_src:
            # 嵌套指标：子契约视图按 dt 拼接（FULL JOIN 保留单边日）
            sql = (
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT COALESCE(n.dt, d.dt) AS dt, "
                f"       n.value::DOUBLE / NULLIF(d.value::DOUBLE, 0) AS value "
                f"FROM {num_src} n FULL OUTER JOIN {den_src} d ON n.dt = d.dt"
            )
        else:
            # 回退：numerator/denominator 引用 dwd 表里的基础事实列
            sql = (
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT strftime({time_col}, '%Y-%m-%d') AS dt, "
                f"       sum({num})::DOUBLE AS numerator, "
                f"       sum({den})::DOUBLE AS denominator, "
                f"       CASE WHEN sum({den}) = 0 THEN NULL "
                f"            ELSE sum({num})::DOUBLE / sum({den})::DOUBLE END AS value "
                f"FROM dwd_base WHERE {where} GROUP BY 1 ORDER BY 1"
            )
    elif contract["type"] in ("sum", "avg"):
        col = contract["numerator"]["metric"]
        agg = "sum" if contract["type"] == "sum" else "avg"
        sql = (
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT strftime({time_col}, '%Y-%m-%d') AS dt, "
            f"       {agg}({col})::DOUBLE AS value "
            f"FROM dwd_base WHERE {where} GROUP BY 1 ORDER BY 1"
        )
    else:  # count
        # P2-04 场景 2/3：声明了 order_key 的 count 契约必须按订单去重——
        # count(DISTINCT order_key) 天然「先过滤后去重」（WHERE 在聚合前生效），
        # 同订单多支付尝试（一行 paid 其余 cancelled）只计一次。
        count_expr = f"count(DISTINCT {order_key})" if order_key else "count(*)"
        sql = (
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT strftime({time_col}, '%Y-%m-%d') AS dt, "
            f"       {count_expr}::DOUBLE AS value "
            f"FROM dwd_base WHERE {where} GROUP BY 1 ORDER BY 1"
        )
    conn.execute(sql)
    # P1-06 ⑤：编译成功才登记——取数端只认已登记 (track, metric)
    register_compiled(ctx_track, name)
    return name
