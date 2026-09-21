#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三级断言引擎测试（架构书 §08.1「断言体系三级一份实现」+ §07 D2「断言同行」+ S1「红灯封下游」）。

对应《施工模型对比赛章程·冻结》§二 考题规格：
- Assertion(name, level, fn, human_text) + AssertionEngine.run(table, conn) → Report
- level ∈ {L1摄取, L2资产, L3指标}；blocking 属性必填
- L1 行数突变带（7 日均值 ±40% 熔断）；L2 唯一性/非空/枚举字典/引用完整；
  L3 跨源对账、环比合理性界（示例实现 ≥1 个）
- blocking 失败 → Report.blocked=True，引擎抛 AssertionBlocked（下游 try 接）
- non-blocking 失败 → 记录不抛
- Report.to_dict() JSON 可序列化：每条 {name, level, blocking, passed, human_text, affected_rows}
- 用例 ≥6 个，其中 ≥3 个负面（断言失败必须被封堵）

运行：仓库根目录下  python tests/test_assertions.py  或  pytest tests/test_assertions.py -q
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent))  # P2-01：稳定 import casekit

import duckdb  # 测试可直连内存库；R5 限制的是 core/storage 之外的 core 代码

import casekit
from core.quality.assertions import (
    AssertionBlocked,
    AssertionEngine,
    cross_source_reconcile,
    enum_values,
    not_null,
    referential_integrity,
    row_count_band,
    trend_reasonableness,
    unique,
)


def _conn() -> duckdb.DuckDBPyConnection:
    """空内存库。"""
    return duckdb.connect(":memory:")


def _seed(conn, sql: str) -> None:
    conn.execute(sql)


# ---------------------------------------------------------------- 正面路径

def a1_unique_pass() -> bool:
    """L2 唯一性：无重复 → passed。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, name VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'c')")
    rep = AssertionEngine([unique("id", table="t")]).run(conn, {"table": "t"})
    r = rep.results[0]
    assert r["passed"] is True and r["name"] == "L2_唯一_id"
    assert r["blocking"] is True and r["affected_rows"] == 0
    return True


def a2_not_null_pass() -> bool:
    """L2 非空：无空值 → passed。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, name VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,'a'),(2,'b')")
    rep = AssertionEngine([not_null("name", table="t")]).run(conn, {"table": "t"})
    assert rep.results[0]["passed"] is True
    return True


def a3_enum_pass() -> bool:
    """L2 枚举字典：值全在字典 → passed。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, status VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,'paid'),(2,'cancelled')")
    rep = AssertionEngine(
        [enum_values("status", {"paid", "cancelled"}, table="t")]
    ).run(conn, {"table": "t"})
    assert rep.results[0]["passed"] is True
    return True


def a4_band_inside() -> bool:
    """L1 行数突变带：当前值在 7 日均值 ±40% 带内 → passed。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER)")
    _seed(conn, "INSERT INTO t SELECT range FROM range(100)")
    history = [100, 105, 98, 102, 101, 99, 100]   # 7 日均值 ≈100
    rep = AssertionEngine([row_count_band(history=history)]).run(conn, {"table": "t"})
    assert rep.results[0]["passed"] is True
    return True


def a5_report_to_dict() -> bool:
    """Report.to_dict() 可 JSON 序列化，字段齐全（name/level/blocking/passed/human_text/affected_rows）。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER)")
    _seed(conn, "INSERT INTO t VALUES (1),(2)")
    rep = AssertionEngine([unique("id", table="t")]).run(conn, {"table": "t"})
    d = json.loads(json.dumps(rep.to_dict()))     # 必须能 JSON 往返
    assert d["blocked"] is False
    r = d["assertions"][0]
    for k in ("name", "level", "blocking", "passed", "human_text", "affected_rows"):
        assert k in r, f"Report 缺字段 {k}"
    assert r["level"] == "L2"
    return True


def a6_referential_pass() -> bool:
    """L2 引用完整：子表无孤儿行 → passed。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE parent (id INTEGER)")
    _seed(conn, "CREATE TABLE child (id INTEGER, pid INTEGER)")
    _seed(conn, "INSERT INTO parent VALUES (1),(2)")
    _seed(conn, "INSERT INTO child VALUES (10,1),(11,2)")
    rep = AssertionEngine(
        [referential_integrity("pid", "parent", "id", table="child")]
    ).run(conn, {"table": "child"})
    assert rep.results[0]["passed"] is True
    return True


def a7_nonblocking_no_raise() -> bool:
    """non-blocking 失败：不抛异常，只记录（L3 环比合理性界示例）。"""
    conn = _conn()
    rep = AssertionEngine(
        [trend_reasonableness("pay_success_rate", current=0.90, previous=0.70, bound=0.10)]
    ).run(conn)
    assert rep.blocked is False
    r = rep.results[0]
    assert r["passed"] is False and r["blocking"] is False
    assert "环比" in r["human_text"]
    return True


def a8_l3_reconcile_pass() -> bool:
    """L3 跨源对账：两表同 key 数值一致 → passed。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE s (k INTEGER, v DOUBLE)")
    _seed(conn, "CREATE TABLE t (k INTEGER, v DOUBLE)")
    _seed(conn, "INSERT INTO s VALUES (1,10.0),(2,20.0)")
    _seed(conn, "INSERT INTO t VALUES (1,10.0),(2,20.0)")
    rep = AssertionEngine(
        [cross_source_reconcile("s", "v", "t", "v", key_col="k")]
    ).run(conn)
    assert rep.results[0]["passed"] is True
    return True


# ---------------------------------------------------------------- 负面路径（熔断）

def r1_unique_dup():
    """L2 唯一性失败（重复 id）→ AssertionBlocked。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, name VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,'a'),(1,'b'),(2,'c')")
    AssertionEngine([unique("id", table="t")]).run(conn, {"table": "t"})


def r2_not_null_blank():
    """L2 非空失败（空值）→ AssertionBlocked。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, name VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,NULL),(2,'b')")
    AssertionEngine([not_null("name", table="t")]).run(conn, {"table": "t"})


def r3_enum_bad_value():
    """L2 枚举失败（字典外值）→ AssertionBlocked。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, status VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,'paid'),(2,'hacked')")
    AssertionEngine([enum_values("status", {"paid", "cancelled"}, table="t")]).run(
        conn, {"table": "t"}
    )


def r4_band_break():
    """L1 行数突变超带（+100%）→ AssertionBlocked。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER)")
    _seed(conn, "INSERT INTO t SELECT range FROM range(200)")
    AssertionEngine([row_count_band(history=[100] * 7)]).run(conn, {"table": "t"})


def r5_referential_orphan():
    """L2 引用完整失败（孤儿行）→ AssertionBlocked。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE parent (id INTEGER)")
    _seed(conn, "CREATE TABLE child (id INTEGER, pid INTEGER)")
    _seed(conn, "INSERT INTO parent VALUES (1)")
    _seed(conn, "INSERT INTO child VALUES (10,1),(11,99)")
    AssertionEngine(
        [referential_integrity("pid", "parent", "id", table="child")]
    ).run(conn, {"table": "child"})


def r6_l3_reconcile_diff():
    """L3 跨源对账不一致 → AssertionBlocked。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE s (k INTEGER, v DOUBLE)")
    _seed(conn, "CREATE TABLE t (k INTEGER, v DOUBLE)")
    _seed(conn, "INSERT INTO s VALUES (1,10.0),(2,20.0)")
    _seed(conn, "INSERT INTO t VALUES (1,10.5),(2,20.0)")   # key=1 偏差 0.5
    AssertionEngine(
        [cross_source_reconcile("s", "v", "t", "v", key_col="k")]
    ).run(conn)


# ---------------------------------------------------------------- 失败单抽检

def f1_blocked_report_names_failed():
    """熔断异常必须携带 Report：blocked=True 且点名失败断言名。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, name VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,'a'),(1,'b')")
    try:
        AssertionEngine([unique("id", table="t"), not_null("name", table="t")]).run(
            conn, {"table": "t"}
        )
    except AssertionBlocked as e:
        assert e.report.blocked is True
        names = [r["name"] for r in e.report.results if not r["passed"]]
        assert "L2_唯一_id" in names, f"异常未点名失败断言: {names}"
        raise                   # 检查完重新抛出，供 casekit 校验消息内容


def f2_enum_affected_rows():
    """枚举失败 affected_rows 必须等于非法行数（负面用例的点名要求）。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER, status VARCHAR)")
    _seed(conn, "INSERT INTO t VALUES (1,'paid'),(2,'hacked'),(3,'hacked')")
    try:
        AssertionEngine([enum_values("status", {"paid"}, table="t")]).run(
            conn, {"table": "t"}
        )
    except AssertionBlocked as e:
        bad = [r for r in e.report.results if not r["passed"]][0]
        assert bad["affected_rows"] == 2, f"affected_rows={bad['affected_rows']}"
        raise                   # 检查完重新抛出，供 casekit 校验消息内容


def f3_band_message():
    """行数突变失败信息必须包含带宽度（±40%）字样，便于下游一眼定位。"""
    conn = _conn()
    _seed(conn, "CREATE TABLE t (id INTEGER)")
    _seed(conn, "INSERT INTO t SELECT range FROM range(500)")
    AssertionEngine([row_count_band(history=[100] * 7)]).run(conn, {"table": "t"})


CASES = (
    ("A1 L2 唯一性通过", a1_unique_pass),
    ("A2 L2 非空通过", a2_not_null_pass),
    ("A3 L2 枚举字典通过", a3_enum_pass),
    ("A4 L1 行数突变带内通过", a4_band_inside),
    ("A5 Report.to_dict() JSON 序列化 + 全字段", a5_report_to_dict),
    ("A6 L2 引用完整通过", a6_referential_pass),
    ("A7 L3 non-blocking 失败记录不抛", a7_nonblocking_no_raise),
    ("A8 L3 跨源对账通过", a8_l3_reconcile_pass),
)

REJECT_CASES: tuple = (
    ("R1 L2 唯一性失败熔断", AssertionBlocked, r1_unique_dup),
    ("R2 L2 非空失败熔断", AssertionBlocked, r2_not_null_blank),
    ("R3 L2 枚举失败熔断", AssertionBlocked, r3_enum_bad_value),
    ("R4 L1 行数突变超带熔断", AssertionBlocked, r4_band_break),
    ("R5 L2 引用完整失败熔断", AssertionBlocked, r5_referential_orphan),
    ("R6 L3 跨源对账不一致熔断", AssertionBlocked, r6_l3_reconcile_diff),
)

FAILMSG_CASES: tuple = (
    ("F1 熔断异常携带 Report 并点名失败断言", f1_blocked_report_names_failed, "L2_唯一_id"),
    ("F2 枚举失败 affected_rows 精确", f2_enum_affected_rows, "影响 2 行"),
    ("F3 行数突变失败信息含带宽度", f3_band_message, "40%"),
)


if __name__ == "__main__":
    sys.exit(casekit.run_cli(
        "TraceableMetrics 三级断言引擎测试（§08.1 · D2 · 红灯封下游）",
        CASES, REJECT_CASES, FAILMSG_CASES))


test_pass, test_reject, test_failmsg = casekit.pytest_cases(
    CASES, REJECT_CASES, FAILMSG_CASES)
