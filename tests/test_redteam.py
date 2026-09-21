#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4④ 双轨隔离红队（架构书 §六 S4 · 宪法 C-2/C-4 · ADR-13 · P8'）。

红队视角：假设攻击者/事故代码持有「本轨上下文」，尝试做下面四件事，全部必须被拒：

  R31  在 core/ 里硬编码对侧轨数据路径（/data/commerce、/data/research）→ 零违例
       （C-4「core 不知道数据住在哪」，R3 门禁静态扫描的独立复扫）
  R32  plugins/commerce ⇄ plugins/research 互相 import（C-2 双轨隔离，
       依赖方向 R2）→ 零违例
  R33  跨轨卷访问：持有 research ctx 读 commerce 卷内路径 → PermissionError
       （C-4 运行时执行者 TrackContext.assert_inside_volume）
  R34  绕过导出桥直连：语义层用对侧 track 取数 → SemanticQueryError；
       无 token 直接调导出桥 → BridgeGateError（C-4「跨轨数据流动仅导出桥一条边」）
  R35  dbt 模型串门防护：双轨共用同一 dbt 工程，run_dbt_model 的 select 默认
       dwd_base 是「危险默认」——所有调用点必须显式声明 select，且不得跨轨
       声明对侧模型（P8' 双轨不互相踩 / S3③ dwd_cohort 接线）

红队与正向测试的差异：正向测试验证「功能可用」，红队验证「越权/越界被拒」。
Docker/网络层隔离（R4 门禁：compose 双栈 + 隔离网络 + bridge-net 禁入）已由
tests/check_dependencies.py 的 R4 静态扫描覆盖，本文件聚焦代码层可执行版。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.bridge import BridgeGateError, export_snapshot  # noqa: E402
from core.ingestion.context import TrackContext  # noqa: E402
from core.semantic import api  # noqa: E402
from core.semantic.compiler import (  # noqa: E402
    ContractError,
    compile_metric,
    load_contract,
)

# ---------------------------------------------------------------- R31：core 跨轨路径零违例

_HARDCODED_DATA_RE = re.compile(r"[\"'](/data/(?:commerce|research)|data[/\\](?:commerce|research))[/\\]")
_CROSS_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)


def _core_py_files() -> list[Path]:
    return sorted((ROOT / "core").rglob("*.py"))


def test_r31_core_has_no_cross_track_paths():
    """C-4/R3：core/ 全量扫描，禁止出现对侧轨硬编码数据路径字面量。"""
    hits = []
    for p in _core_py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in _HARDCODED_DATA_RE.finditer(text):
            hits.append(f"{p.relative_to(ROOT)}: {m.group(1)}")
    assert hits == [], f"core 内发现跨轨硬编码路径（C-4 违例）:\n" + "\n".join(hits)


# ---------------------------------------------------------------- R32：plugins 双轨互引零违例

def test_r32_plugins_no_cross_track_imports():
    """C-2/R2：commerce 与 research 互相不得 import；core 不得 import plugins。"""
    violations = []
    for track in ("commerce", "research"):
        for p in sorted((ROOT / "plugins" / track).rglob("*.py")):
            text = p.read_text(encoding="utf-8", errors="ignore")
            for m in _CROSS_IMPORT_RE.finditer(text):
                mod = m.group(1)
                other = "research" if track == "commerce" else "commerce"
                if mod.startswith(f"plugins.{other}") or mod == "plugins":
                    violations.append(f"R2 cross-plugin: {p.relative_to(ROOT)} imports {mod}")
    # core → plugins（R1 依赖方向，红队复扫）
    for p in _core_py_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in _CROSS_IMPORT_RE.finditer(text):
            if m.group(1).startswith("plugins"):
                violations.append(f"R1 core→plugins: {p.relative_to(ROOT)} imports {m.group(1)}")
    assert violations == [], "双轨互引违例（C-2/R1/R2）:\n" + "\n".join(violations)


# ---------------------------------------------------------------- R33：跨轨卷访问被拒

def test_r33_cross_track_volume_access_blocked(tmp_path):
    """C-4：持有 research ctx，读/写 commerce 卷内任何路径 → PermissionError（双向）。"""
    v_commerce = tmp_path / "v_commerce"
    v_research = tmp_path / "v_research"
    v_commerce.mkdir()
    v_research.mkdir()

    commerce_ctx = TrackContext(track="commerce", volume_root=v_commerce)
    research_ctx = TrackContext(track="research", volume_root=v_research)

    # 攻击向量 1：research ctx 试图引用 commerce 卷内文件（读方向）
    foreign_file = commerce_ctx.volume_root / "raw" / "orders.csv"
    foreign_file.parent.mkdir(parents=True)
    foreign_file.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError, match="outside volume"):
        research_ctx.assert_inside_volume(foreign_file)

    # 攻击向量 2：research ctx 试图写 commerce 卷（assert_inside_volume 拒绝后无法落地）
    with pytest.raises(PermissionError, match="outside volume"):
        research_ctx.assert_inside_volume(commerce_ctx.db_path)

    # 攻击向量 3：反向——commerce ctx 访问 research 卷同样被拒
    with pytest.raises(PermissionError, match="outside volume"):
        commerce_ctx.assert_inside_volume(research_ctx.db_path)

    # 反向确认：本轨内路径放行（隔离不误伤）
    assert research_ctx.assert_inside_volume(research_ctx.db_path) == research_ctx.db_path.resolve()


def test_r33_track_context_rejects_traversal():
    """C-4：TrackContext 构造时拒绝卷根含 .. 穿越段（防卷根本身逃逸）。"""
    with pytest.raises(ValueError, match="traversal"):
        TrackContext(track="commerce", volume_root=Path("/tmp/../etc"))


# ---------------------------------------------------------------- R34：绕过导出桥直连被拒

def test_r34_bridge_rejects_export_without_token(tmp_path):
    """C-4「跨轨数据流动仅导出桥一条边」：无/伪造 token 直调导出桥 → 闸1拒绝。"""
    v_src = tmp_path / "v_src"
    v_tgt = tmp_path / "v_tgt"
    bridge = tmp_path / "bridge"
    for d in (v_src, v_tgt, bridge):
        d.mkdir()

    src_ctx = TrackContext(track="commerce", volume_root=v_src)
    tgt_ctx = TrackContext(track="research", volume_root=v_tgt)

    with pytest.raises(BridgeGateError, match="token"):
        export_snapshot(
            token_id="tk-forged-no-approval",
            purpose="red_team_probe",
            source_ctx=src_ctx,
            target_ctx=tgt_ctx,
            bridge_dir=bridge,
            source_ledger_db=v_src / "ledger.db",
            target_ledger_db=v_tgt / "ledger.db",
            run_id="r-red-team-0001",
        )


def test_r34_semantic_layer_rejects_cross_track_query(tmp_path):
    """P1-06 ⑤⑥：语义层取数只认本轨已编译登记的 metric——跨轨取数 → SemanticQueryError。"""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE dwd_base (created_at TIMESTAMP, value DOUBLE)")
    con.execute("INSERT INTO dwd_base VALUES (DATE '2026-08-26', 3.0)")
    contract = tmp_path / "redteam_metric.yml"
    contract.write_text(
        "metric: redteam_metric\ntype: count\ngrain: order\n"
        "time_column: created_at\nfilters: []\nversion: 1\n",
        encoding="utf-8",
    )
    ledger_db = tmp_path / "ledger.db"
    try:
        # 只在 research 轨编译登记
        compile_metric(con, "research", load_contract(contract))
        # research 轨自己取数 → 放行（count 契约对每行计数，1 行 → 1.0）
        q = api.query_metric(con, ledger_db, contract, "research")
        assert q["rows"] == [["2026-08-26", 1.0]]
        # 攻击向量：commerce 轨（对侧）绕过导出桥直连取数 → 拒绝
        with pytest.raises(api.SemanticQueryError):
            api.query_metric(con, ledger_db, contract, "commerce")
    finally:
        con.close()


# ---------------------------------------------------------------- R35：dbt 模型串门防护

_RUN_DBT_CALL_RE = re.compile(r"run_dbt_model\s*\(")


def _extract_call_args(text: str, start: int) -> str:
    """从 '(' 之后的位置按括号配对提取调用实参（处理 Path(...) 等嵌套括号）。

    start 是外层 '(' 已消费后的位置，故初始深度 = 1。
    """
    depth = 1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i]
    return text[start:]


def _dbt_call_sites() -> list[tuple[Path, str]]:
    """扫描 plugins/ 下所有 run_dbt_model 调用点（红队关心的攻击面）。"""
    sites = []
    for p in sorted((ROOT / "plugins").rglob("*.py")):
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in _RUN_DBT_CALL_RE.finditer(text):
            sites.append((p, _extract_call_args(text, m.end())))
    return sites


def test_r35_dbt_calls_always_explicit_select():
    """P8'/S3③：双轨共用 dbt 工程——调用 run_dbt_model 必须显式声明 select，
    禁止省略（默认 dwd_base 是危险默认：research 轨省略就会把 dwd_base 物化进来 =
    模型串门）。"""
    sites = _dbt_call_sites()
    assert sites, "红队前提失效：plugins/ 下找不到 run_dbt_model 调用点"

    implicit = []
    for p, args in sites:
        if "select=" not in args:
            implicit.append(f"{p.relative_to(ROOT)}: 省略 select（危险默认 dwd_base）")
    assert implicit == [], "发现省略 select 的 dbt 调用（模型串门风险）:\n" + "\n".join(implicit)


def test_r35_dbt_select_matches_track():
    """P8'：调用方声明轨与 select 的模型必须同轨——commerce 只允许 dwd_base、
    research 只允许 dwd_cohort；跨轨声明（commerce 跑 dwd_cohort 等）即违例。"""
    allowed = {"commerce": {"dwd_base"}, "research": {"dwd_cohort"}}
    mismatches = []
    for p, args in _dbt_call_sites():
        rel = p.relative_to(ROOT)
        track = "commerce" if "plugins/commerce" in rel.as_posix() else (
            "research" if "plugins/research" in rel.as_posix() else None)
        if track is None:
            continue  # 非轨插件（如 admin）不参与建模
        m = re.search(r"select\s*=\s*[\"']([\w]+)[\"']", args)
        if not m:
            mismatches.append(f"{rel}: select 参数无法解析")
            continue
        if m.group(1) not in allowed[track]:
            mismatches.append(f"{rel}: select={m.group(1)!r} 与轨 {track!r} 不匹配")
    assert mismatches == [], "dbt 模型串门违例（P8'）:\n" + "\n".join(mismatches)


# ---------------------------------------------------------------- 汇总

def test_redteam_all_vectors_present():
    """红队完整性：四个攻击面全部有对应测试存在（防止红队测试被误删）。"""
    names = {n for n in globals() if n.startswith("test_r3") or n.startswith("test_r35")}
    for expect in (
        "test_r31_core_has_no_cross_track_paths",
        "test_r32_plugins_no_cross_track_imports",
        "test_r33_cross_track_volume_access_blocked",
        "test_r33_track_context_rejects_traversal",
        "test_r34_bridge_rejects_export_without_token",
        "test_r34_semantic_layer_rejects_cross_track_query",
        "test_r35_dbt_calls_always_explicit_select",
        "test_r35_dbt_select_matches_track",
    ):
        assert expect in names, f"红队测试缺失: {expect}"
