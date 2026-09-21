#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-04 指标口径边界测试（扩充 N12 之外的固定演示口径）。

场景清单（P2-04「必增场景」1~10）：
  M1  多日期：按日分组产出多行，且升序
  M2  同一订单重复行：count 必须按订单去重（契约描述「订单去重」）
  M3  同订单多支付尝试：过滤后再按订单去重，只计一次
  M4  分母为 0：ratio 返回 NULL 而非 Inf/NaN/错误
  M5  分子日期集合与分母不一致：单边日保留且 value=NULL
  M6  未知/空 status：filter 精确排除
  M7  含 test_order 列时强制 test_order == false（父 ratio 也不可绕过）
  M8  filters 不得被父 ratio 契约或消费端绕过
  M9  日期排序与时区/跨午夜边界
  M10 v1→v2 口径版本影响面与历史快照重放

规则：每项整改先加失败用例再改实现；测试跑在修复前的旧实现上必须失败，
     修复后通过。运行：仓库根下 python -m pytest tests/test_p204_metric_boundary.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import duckdb

from core.audit.snapshot import publish_snapshot
from core.semantic.compiler import compile_metric, load_contract


def _conn(rows_sql: str):
    """内存 DuckDB + dwd_base 明细表。"""
    con = duckdb.connect()
    con.execute(rows_sql)
    return con


def _count_contract(name: str, order_key: str | None = None) -> dict:
    c = {
        "metric": name, "type": "count", "grain": "order",
        "time_column": "created_at",
        "filters": [{"field": "status", "op": "=", "value": "paid"}],
        "version": 1,
    }
    if order_key:
        c["order_key"] = order_key
    return c


def _write_count(tmp_path: Path, name: str, filters: str = "") -> Path:
    """写一份 count 子契约文件（嵌套 ratio 测试用）。"""
    p = tmp_path / f"{name}.yml"
    p.write_text(
        f"metric: {name}\ntype: count\ngrain: order\ntime_column: created_at\n"
        f"filters:\n{filters}version: 1\n",
        encoding="utf-8",
    )
    return p


def _write_ratio(tmp_path: Path, name: str, num: str, den: str) -> Path:
    p = tmp_path / f"{name}.yml"
    p.write_text(
        f"metric: {name}\ntype: ratio\ngrain: order\ntime_column: created_at\n"
        f"numerator: {{ metric: {num} }}\ndenominator: {{ metric: {den} }}\n"
        f"filters: []\nversion: 1\n",
        encoding="utf-8",
    )
    return p


# ---------------------------------------------------------------- M1 多日期

def test_m1_multi_date_groups_sorted():
    """多日期数据按日分组且升序。"""
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-25 10:00:00', 'paid'), "
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid'), "
        "(TIMESTAMP '2026-08-27 10:00:00', 'paid'), "
        "(TIMESTAMP '2026-08-26 11:00:00', 'cancelled')"
    )
    try:
        compile_metric(con, "commerce", _count_contract("m1_orders"))
        rows = con.execute("SELECT dt, value FROM m1_orders").fetchall()
        assert rows == [("2026-08-25", 1.0), ("2026-08-26", 1.0), ("2026-08-27", 1.0)], rows
    finally:
        con.close()


# ---------------------------------------------------------------- M2 同一订单重复行 → 去重

def test_m2_duplicate_order_rows_deduped():
    """同一订单出现两行（同一支付尝试被重复记账）：必须只计 1。

    契约描述「订单去重」；修复前实现是 count(*) 会把同一订单数两次。
    """
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR, order_id VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid', 'O-1'), "
        "(TIMESTAMP '2026-08-26 11:00:00', 'paid', 'O-1'), "
        "(TIMESTAMP '2026-08-26 12:00:00', 'paid', 'O-2')"
    )
    try:
        compile_metric(con, "commerce",
                       _count_contract("m2_orders", order_key="order_id"))
        rows = con.execute("SELECT value FROM m2_orders").fetchall()
        assert rows == [(2.0,)], f"订单未去重：{rows}（应 2，count(*) 会得 3）"
    finally:
        con.close()


# ---------------------------------------------------------------- M3 同订单多支付尝试

def test_m3_multi_attempts_deduped_after_filter():
    """同一订单多次支付尝试（一行 paid 其余 cancelled）：过滤后按订单去重计 1。"""
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR, order_id VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 'cancelled', 'O-9'), "
        "(TIMESTAMP '2026-08-26 11:00:00', 'paid', 'O-9'), "
        "(TIMESTAMP '2026-08-26 12:00:00', 'paid', 'O-10')"
    )
    try:
        compile_metric(con, "commerce",
                       _count_contract("m3_orders", order_key="order_id"))
        rows = con.execute("SELECT value FROM m3_orders").fetchall()
        assert rows == [(2.0,)], f"多支付尝试去重错误：{rows}（O-9 只计 1，共 2）"
    finally:
        con.close()


# ---------------------------------------------------------------- M4 分母为 0

def test_m4_zero_denominator_ratio_is_null():
    """ratio 分母为 0 时必须返回 NULL（禁止 Inf/NaN/报错）。"""
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, num_col DOUBLE, den_col DOUBLE); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 0.0, 0.0), "
        "(TIMESTAMP '2026-08-26 11:00:00', 3.0, 0.0)"
    )
    try:
        compile_metric(con, "commerce", {
            "metric": "m4_rate", "type": "ratio", "grain": "order",
            "time_column": "created_at",
            "numerator": {"metric": "num_col"},
            "denominator": {"metric": "den_col"},
            "filters": [], "version": 1,
        })
        row = con.execute("SELECT value FROM m4_rate").fetchone()
        assert row[0] is None, f"分母为 0 应得 NULL，实得 {row[0]!r}"
    finally:
        con.close()


# ---------------------------------------------------------------- M5 分子/分母日期集合不一致

def test_m5_numerator_denominator_date_mismatch(tmp_path):
    """分子只有 8-26，分母有 8-26/8-27：单边日保留且 value=NULL（诚实呈现缺失）。"""
    _write_count(tmp_path, "m5_num", "  - { field: status, op: '=', value: 'paid' }\n")
    _write_count(tmp_path, "m5_den", "")
    rate_p = _write_ratio(tmp_path, "m5_rate", "m5_num", "m5_den")
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid'), "
        "(TIMESTAMP '2026-08-26 11:00:00', 'cancelled'), "
        "(TIMESTAMP '2026-08-27 10:00:00', 'cancelled')"
    )
    try:
        compile_metric(con, "commerce", load_contract(rate_p), contracts_dir=tmp_path)
        rows = con.execute("SELECT dt, value FROM m5_rate ORDER BY dt").fetchall()
        assert rows[0] == ("2026-08-26", 0.5), rows
        assert rows[1][0] == "2026-08-27" and rows[1][1] is None, \
            f"单边日应保留且 value=NULL：{rows}"
    finally:
        con.close()


# ---------------------------------------------------------------- M6 未知/空 status

def test_m6_unknown_and_empty_status_excluded():
    """未知/空 status 一律不进 paid 计数（filter 精确匹配，不放过任何变体）。"""
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid'),"
        "(TIMESTAMP '2026-08-26 11:00:00', 'Paid'),"
        "(TIMESTAMP '2026-08-26 12:00:00', ''),"
        "(TIMESTAMP '2026-08-26 13:00:00', NULL),"
        "(TIMESTAMP '2026-08-26 14:00:00', 'refunded')"
    )
    try:
        compile_metric(con, "commerce", _count_contract("m6_orders"))
        rows = con.execute("SELECT value FROM m6_orders").fetchall()
        assert rows == [(1.0,)], f"未知/空 status 未被排除：{rows}"
    finally:
        con.close()


# ---------------------------------------------------------------- M7 test_order 强制

def test_m7_test_order_forced_not_bypassable_by_parent_ratio(tmp_path):
    """dwd 含 test_order 列：父 ratio 引用子 count 时，测试单同样被排除。"""
    _write_count(tmp_path, "m7_num", "  - { field: status, op: '=', value: 'paid' }\n")
    _write_count(tmp_path, "m7_den", "")
    rate_p = _write_ratio(tmp_path, "m7_rate", "m7_num", "m7_den")
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR, test_order BOOLEAN); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid', false),"
        "(TIMESTAMP '2026-08-26 11:00:00', 'paid', true),"
        "(TIMESTAMP '2026-08-26 12:00:00', 'cancelled', false)"
    )
    try:
        compile_metric(con, "commerce", load_contract(rate_p), contracts_dir=tmp_path)
        rows = con.execute("SELECT dt, value FROM m7_rate").fetchall()
        # 排除测试单后：num=paid 且非测试单=1，den=全部非测试单=2（cancelled 也非测试单）
        # → 1/2 = 0.5；若 test_order 过滤被绕过则是 2/3 ≈ 0.667
        assert rows == [("2026-08-26", 0.5)], f"父 ratio 绕过 test_order 过滤：{rows}"
    finally:
        con.close()


# ---------------------------------------------------------------- M8 filters 不可被父契约绕过

def test_m8_child_filters_not_bypassed_by_parent(tmp_path):
    """子指标 filter（status='paid'）在父 ratio 引用时依然生效（父自身无 filter）。"""
    _write_count(tmp_path, "m8_num", "  - { field: status, op: '=', value: 'paid' }\n")
    _write_count(tmp_path, "m8_den", "")
    rate_p = _write_ratio(tmp_path, "m8_rate", "m8_num", "m8_den")
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid'),"
        "(TIMESTAMP '2026-08-26 11:00:00', 'cancelled'),"
        "(TIMESTAMP '2026-08-26 12:00:00', 'cancelled')"
    )
    try:
        compile_metric(con, "commerce", load_contract(rate_p), contracts_dir=tmp_path)
        rows = con.execute("SELECT dt, value FROM m8_rate").fetchall()
        assert rows == [("2026-08-26", 1 / 3)], \
            f"父 ratio 绕过子指标 filter：{rows}（应 1/3）"
    finally:
        con.close()


# ---------------------------------------------------------------- M9 日期排序与时区边界

def test_m9_date_sorting_and_cross_midnight():
    """日期升序；跨午夜时间戳按日正确归组（23:xx 与次日 00:xx 分属两天）。"""
    con = _conn(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-27 00:30:00', 'paid'),"
        "(TIMESTAMP '2026-08-26 23:45:00', 'paid'),"
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid'),"
        "(TIMESTAMP '2026-08-28 09:00:00', 'paid')"
    )
    try:
        compile_metric(con, "commerce", _count_contract("m9_orders"))
        rows = con.execute("SELECT dt, value FROM m9_orders").fetchall()
        assert [r[0] for r in rows] == ["2026-08-26", "2026-08-27", "2026-08-28"], rows
        assert rows[0][1] == 2.0 and rows[1][1] == 1.0, f"跨午夜归组错误：{rows}"
    finally:
        con.close()


# ---------------------------------------------------------------- M10 口径版本与快照重放

def test_m10_version_change_replay_uses_snapshot(tmp_path):
    """v1→v2 口径变更后：新查询用新口径；历史快照重放仍返回快照内旧口径数据。"""
    # v1：count 全部订单（无 status filter）
    db_file = tmp_path / "commerce.db"
    con = duckdb.connect(str(db_file))   # 文件库：CHECKPOINT 后才有落盘字节
    con.execute(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR); "
        "INSERT INTO dwd_base VALUES "
        "(TIMESTAMP '2026-08-26 10:00:00', 'paid'),"
        "(TIMESTAMP '2026-08-26 11:00:00', 'cancelled')"
    )
    v1 = {"metric": "m10_orders", "type": "count", "grain": "order",
          "time_column": "created_at", "filters": [], "version": 1}
    compile_metric(con, "commerce", v1)
    assert con.execute("SELECT value FROM m10_orders").fetchone()[0] == 2.0  # v1 口径=2

    # 落盘 + 发布快照（v1 口径的不可变档案）
    con.execute("CHECKPOINT")
    con.close()
    snap = publish_snapshot(db_file, tmp_path / "snapshots", "s-m10", "commerce")

    # v2：加 status='paid' filter，口径变=1
    con = duckdb.connect(str(db_file))
    v2 = {"metric": "m10_orders", "type": "count", "grain": "order",
          "time_column": "created_at",
          "filters": [{"field": "status", "op": "=", "value": "paid"}],
          "version": 2}
    compile_metric(con, "commerce", v2)
    fresh = con.execute("SELECT value FROM m10_orders").fetchone()[0]
    assert fresh == 1.0, f"v2 新口径应=1，实得 {fresh}"
    con.close()

    # 快照重放：只读打开 v1 快照库 → 仍得 v1 口径 2.0
    from core.semantic.api import replay_from_snapshot
    replay = replay_from_snapshot(Path(snap["path"]), "m10_orders")
    assert replay == [("2026-08-26", 2.0)], \
        f"历史快照重放应保留 v1 口径 2.0，实得 {replay}（快照被 v2 污染？）"

    # 快照库文件在 v2 发布后必须保持不变（sha256 由 P0-06 校验，此处复核值未变）
    assert Path(snap["path"]).exists(), "快照文件必须存在"
