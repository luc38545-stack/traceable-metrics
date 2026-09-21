#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""face=R 独立入口测试（S3 论文轨 · 架构书 §六「独立入口」）。

webapp_r 是研究者专用界面，与 commerce webapp（face=C）严格隔离：
  - 独立端口 8766（commerce=8765）、独立 CSRF 启动令牌；
  - 独立数据卷（ROOT/data/research，含 uploads/runs/snapshots/repro/exports）；
  - 主旅程：上传三件套（队列 CSV + 数据集清单 + 预注册台账）→ 选 plan_id →
    跑管道（复用 run_research_pipeline）→ 报告预览 → 导出（Quarto / 复现包）。

设计纪律（与 commerce webapp 一致）：
  - 上传原样落盘 + 原始字节 sha256 指纹（P1-08），索引只存相对文件名（P1-03）；
  - 变更操作一律带 CSRF 启动令牌（P1-05）；跨源/限速防护；
  - 上传索引损坏 → 显性拒绝（P4，不静默重置）；
  - run 失败必须进 append-only 事件流（P0-03，管道内部 write_run_failed）；
  - 并发 run 拒绝（409 语义，共享 DuckDB 目标）；幂等键防重复建批次。

casekit 约定：REJECT/FAILMSG 函数必须让异常自然上抛，不得函数内捕获后 return。

实现纪律（测试先行 RED）：本文件先落盘，plugins/research/webapp_r.py 未实现时
逐条 FAIL；实现后转 GREEN。

运行：仓库根目录下  python tests/test_research_webapp.py  或
      pytest -q tests/test_research_webapp.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH（README 已记录）
sys.path.insert(0, str(Path(__file__).resolve().parent))  # P2-01：稳定 import casekit

import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import casekit
import pytest

# 本机全局代理坑（见记忆）：访问 127.0.0.1 必须绕过代理，否则请求被劫持返回 404/502
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)


# ---------------------------------------------------------------- 共享夹具

def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="traceable_wr_"))


def _make_plan() -> dict:
    return {
        "plan_id": "pre-w-001",
        "hypothesis": "处理组转化率高于对照组",
        "outcome": "binary",
        "design": "rct",
        "method": "two_proportion_test",
        "alpha": 0.05,
        "mde": 0.08,
        "power": 0.8,
        "primary_metric": "conversion_rate",
        "epsilon": 1.0,
    }


def _manifest(missing: str | None = None) -> dict:
    m = {
        "dataset_name": "cohort-w-2026",
        "version": "1.0.0",
        "owner": "research-lab",
        "collection_method": "platform log extraction, de-identified at source",
        "irb": {"approved": True, "expires_at": "2099-12-31",
                "approval_no": "IRB-2026-0088"},
        "privacy": {"quasi_identifiers": ["city", "age"], "k": 2,
                    "dp_epsilon": 1.0},
    }
    if missing:
        m.pop(missing, None)
    return m


def _cohort_csv(path: Path) -> Path:
    """650+650 队列：a 臂 325 转化（i%2==0），b 臂 217 转化（i%3==0）。"""
    cities, ages = ["北京", "上海", "广州"], [25, 35]
    rows = [("participant_id", "arm", "converted", "city", "age")]
    for i in range(650):
        rows.append((f"p{i + 1:04d}", "a", 1 if i % 2 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    for i in range(650):
        rows.append((f"p{i + 651:04d}", "b", 1 if i % 3 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    path.write_text("\n".join(",".join(map(str, r)) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def _make_prereg(path: Path, plan: dict) -> Path:
    from core.statistics.prereg import PreRegistrationLedger
    PreRegistrationLedger(path).register(plan)
    return path


def _upload_all(wr, root: Path) -> dict:
    """上传三件套，返回 {csv_id, manifest_id, prereg_id, plan_id}。"""
    raw = _cohort_csv(root / "cohort.csv")
    csv_up = wr.action_upload("csv", "cohort.csv", raw.read_bytes())
    man = _manifest()
    man_up = wr.action_upload("manifest", "dataset.json",
                              json.dumps(man).encode("utf-8"))
    prereg = _make_prereg(root / "prereg.json", _make_plan())
    pre_up = wr.action_upload("prereg", "prereg.json",
                              prereg.read_bytes())
    return {"csv_id": csv_up["upload_id"], "manifest_id": man_up["upload_id"],
            "prereg_id": pre_up["upload_id"], "plan_id": "pre-w-001"}


def _prepare_webapp(tmp: Path):
    """把 webapp_r 的 ROOT 指到隔离目录（路径运行时派生，自动跟随），返回模块。"""
    import plugins.research.webapp_r as wr
    wr.ROOT = tmp
    (tmp / "data" / "research" / "uploads").mkdir(parents=True, exist_ok=True)
    wr._state = {"runs": [], "uploads": {}}
    wr._IDEMPOTENCY.clear()
    wr._RATE.clear()
    return wr


# ---------------------------------------------------------------- W1-W4 基础

def w1_independent_port_and_csrf() -> bool:
    """face=R 独立入口：端口必须 8766，CSRF 令牌独立于 commerce webapp。"""
    import plugins.commerce.webapp as cw
    import plugins.research.webapp_r as wr

    assert wr.PORT == 8766, f"研究轨必须独立端口 8766，实为 {wr.PORT}"
    assert cw.PORT == 8765, f"商业轨端口应为 8765，实为 {cw.PORT}"
    tok = wr._CSRF_TOKEN
    assert len(tok) == 32, "CSRF 启动令牌必须是 32 hex（secrets.token_hex(16)）"
    assert tok != cw._CSRF_TOKEN, "研究轨 CSRF 必须独立于 commerce，不得共用"
    return True


def w2_upload_csv_original_bytes() -> bool:
    """CSV 上传：原样落盘（sha256 一致）+ 索引记录相对文件名。"""
    import hashlib

    wr = _prepare_webapp(_tmpdir())
    root = wr.ROOT
    raw = _cohort_csv(root / "cohort.csv")
    content = raw.read_bytes()
    up = wr.action_upload("csv", "cohort.csv", content)
    uid = up["upload_id"]
    assert up["sha256"] == hashlib.sha256(content).hexdigest(), "指纹必须等于原始字节"
    p = Path(root) / "data" / "research" / "uploads" / uid
    assert p.read_bytes() == content, "上传文件必须原样落盘，不得改动字节"
    idx = json.loads(wr._index_path().read_text(encoding="utf-8"))
    ent = idx[uid]
    assert ent["file"] == uid and ent["sha256"] == up["sha256"]
    assert Path(ent["file"]).name == ent["file"], "索引不得含路径成分（P1-03）"
    return True


def w3_upload_manifest_parsed() -> bool:
    """清单上传：解析出四要素 + IRB + privacy 摘要。"""
    wr = _prepare_webapp(_tmpdir())
    content = json.dumps(_manifest()).encode("utf-8")
    up = wr.action_upload("manifest", "dataset.json", content)
    assert up["kind"] == "manifest"
    s = up["summary"]
    assert s["dataset_name"] == "cohort-w-2026"
    assert s["irb"]["approved"] is True
    assert s["privacy"]["k"] == 2
    return True


def w4_prereg_options_list() -> bool:
    """预注册选项：从台账列出 plan_id + 假设，供前端选择。"""
    wr = _prepare_webapp(_tmpdir())
    prereg = _make_prereg(Path(wr.ROOT) / "prereg.json", _make_plan())
    up = wr.action_upload("prereg", "prereg.json", prereg.read_bytes())
    opts = wr.action_prereg_options(up["upload_id"])
    assert any(r["plan_id"] == "pre-w-001" and "对照组" in r["hypothesis"]
               for r in opts["records"]), "必须能列出预注册计划"
    return True


# ---------------------------------------------------------------- W5-W11 管道

def w5_run_e2e_research() -> bool:
    """端到端：三件套 → action_run → GREEN / claims clean / 快照 / 复现包 / Quarto。

    计数必须来自数据本身（(325, 650, 217, 650)）；DP 预算 spend 0.1 后剩 0.9；
    台账 SQLite 可机查且 prereg_checked 事件入流。
    """
    import sqlite3

    wr = _prepare_webapp(_tmpdir())
    up = _upload_all(wr, wr.ROOT)
    out = wr.action_run(
        up["csv_id"], up["manifest_id"], up["prereg_id"], up["plan_id"],
        question="arm conversion", period="2026-08", mde=0.08,
        dp_budget_path=Path(wr.ROOT) / "data" / "research" / "dp.json",
        dp_epsilon=0.1,
    )
    assert out["run_id"].startswith("r-"), "必须产出 run_id"
    assert out["conclusion_status"] == "GREEN", (
        f"样本量满足 mde=0.08 应为 GREEN：{out['conclusion_status']}")
    assert out["reconcile_diff"] == "clean", "claims 对账必须 clean"
    assert list(out["counts"]) == [325, 650, 217, 650], "计数必须来自数据"
    assert out["dp_remaining"] is not None and abs(out["dp_remaining"] - 0.9) < 1e-9
    # 快照 / 复现包 / Quarto 真实存在（研究卷内）
    snap_dir = Path(wr.ROOT) / "data" / "research" / "snapshots" / out["snapshot_id"]
    assert snap_dir.exists(), "快照目录必须存在"
    assert Path(out["repro_package"]).exists(), "复现包必须存在"
    assert Path(out["qmd_path"]).name.startswith("[R]"), "Quarto 出版缺失"
    # 台账机查 + 事件流
    ledger_db = Path(wr.ROOT) / "data" / "research" / "ledger.db"
    conn = sqlite3.connect(str(ledger_db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='prereg_checked'"
                         " AND subject_id=?", (out["run_id"],)).fetchone()[0]
        runs = conn.execute("SELECT track FROM runs WHERE run_id=?",
                            (out["run_id"],)).fetchone()
    finally:
        conn.close()
    assert n >= 1, "prereg_checked 必须进 append-only 事件流"
    assert runs and runs[0] == "research"
    return True


def w6_report_preview() -> bool:
    """报告预览：读台账回显结论/计数/对账/步骤/DP 剩余。"""
    wr = _prepare_webapp(_tmpdir())
    up = _upload_all(wr, wr.ROOT)
    out = wr.action_run(up["csv_id"], up["manifest_id"], up["prereg_id"],
                        up["plan_id"], mde=0.08)
    rep = wr.action_report(out["run_id"])
    assert rep["conclusion_status"] == "GREEN"
    assert list(rep["counts"]) == [325, 650, 217, 650]
    assert rep["reconcile_diff"] == "clean"
    assert rep["steps"] and rep["steps"][-1]["ok"], "步骤必须完整且全 OK"
    return True


def w7_export_qmd() -> bool:
    """导出 Quarto：[R] 文件内容非空，数字走插值（正文不含硬编码 effect 值）。"""
    wr = _prepare_webapp(_tmpdir())
    up = _upload_all(wr, wr.ROOT)
    out = wr.action_run(up["csv_id"], up["manifest_id"], up["prereg_id"],
                        up["plan_id"], mde=0.08)
    ex = wr.action_export_qmd(out["run_id"])
    assert ex["file_name"].startswith("[R]") and ex["file_name"].endswith(".qmd")
    assert ex["sha256"], "导出必须带指纹（D8）"
    text = ex["content"].decode("utf-8")
    assert "{effect_value}" in text or "{p_value}" in text, "结论数字必须走插值"
    return True


def w8_export_repro_zip() -> bool:
    """导出复现包：zip 内容（PK 头）+ 清单 sha256 校验。"""
    import hashlib

    wr = _prepare_webapp(_tmpdir())
    up = _upload_all(wr, wr.ROOT)
    out = wr.action_run(up["csv_id"], up["manifest_id"], up["prereg_id"],
                        up["plan_id"], mde=0.08)
    ex = wr.action_export_repro(out["run_id"])
    assert ex["file_name"].endswith(".zip")
    assert ex["content"][:2] == b"PK", "复现包必须是 zip"
    assert ex["sha256"] == hashlib.sha256(ex["content"]).hexdigest()
    return True


def w9_idempotency_key() -> bool:
    """幂等键：同 key 两次 run 返回同一 run_id，不重复建批次。"""
    wr = _prepare_webapp(_tmpdir())
    up = _upload_all(wr, wr.ROOT)
    a = wr.action_run(up["csv_id"], up["manifest_id"], up["prereg_id"],
                      up["plan_id"], mde=0.08, idempotency_key="w9-key")
    b = wr.action_run(up["csv_id"], up["manifest_id"], up["prereg_id"],
                      up["plan_id"], mde=0.08, idempotency_key="w9-key")
    assert a["run_id"] == b["run_id"], "幂等键必须返回首次结果"
    return True


def w10_history() -> bool:
    """历史列表包含刚才的 run。"""
    wr = _prepare_webapp(_tmpdir())
    up = _upload_all(wr, wr.ROOT)
    out = wr.action_run(up["csv_id"], up["manifest_id"], up["prereg_id"],
                        up["plan_id"], mde=0.08)
    hist = wr.action_history()
    assert any(r["run_id"] == out["run_id"] for r in hist["runs"])
    return True


def w11_failure_registered_audit() -> bool:
    """失败显性登记（P0-03）：缺要素清单 → DatasetGateError，且 run_failed 进事件流。"""
    import sqlite3

    from plugins.research.dataset_gate import DatasetGateError

    wr = _prepare_webapp(_tmpdir())
    root = wr.ROOT
    raw = _cohort_csv(root / "cohort.csv")
    csv_up = wr.action_upload("csv", "cohort.csv", raw.read_bytes())
    bad_man = _manifest(missing="owner")
    man_up = wr.action_upload("manifest", "dataset.json",
                              json.dumps(bad_man).encode("utf-8"))
    prereg = _make_prereg(root / "prereg.json", _make_plan())
    pre_up = wr.action_upload("prereg", "prereg.json", prereg.read_bytes())
    try:
        wr.action_run(csv_up["upload_id"], man_up["upload_id"],
                      pre_up["upload_id"], "pre-w-001", mde=0.08)
        return False  # 应当抛 DatasetGateError
    except DatasetGateError:
        pass
    ledger_db = root / "data" / "research" / "ledger.db"
    conn = sqlite3.connect(str(ledger_db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='run_failed'"
            " AND track='research'").fetchone()[0]
    finally:
        conn.close()
    assert n >= 1, "失败 run 必须显性登记进 append-only 事件流（P0-03）"
    return True


# ---------------------------------------------------------------- W12-W15 拒绝

def w12_run_without_upload_rejected() -> bool:
    """未上传三件套直接 run → 显性 ValueError（点名缺什么）。"""
    wr = _prepare_webapp(_tmpdir())
    wr.action_run("no-such-csv", "no-such-manifest", "no-such-prereg",
                  "pre-w-001")  # 必须抛 ValueError，不会走到这里
    return True


def w13_manifest_missing_element_rejected() -> bool:
    """清单缺四要素 → 数据集 gate 显性拒绝（DatasetGateError）。"""
    wr = _prepare_webapp(_tmpdir())
    up = _upload_all(wr, wr.ROOT)
    bad_man = _manifest(missing="owner")
    man_up = wr.action_upload("manifest", "dataset.json",
                              json.dumps(bad_man).encode("utf-8"))
    wr.action_run(up["csv_id"], man_up["upload_id"], up["prereg_id"],
                  up["plan_id"], mde=0.08)  # 必须抛 DatasetGateError
    return True


def w14_concurrent_run_rejected() -> bool:
    """并发 run：锁被占用 → RunConflict（共享 DuckDB 目标，拒绝并行）。"""
    import plugins.research.webapp_r as wr

    wr = _prepare_webapp(_tmpdir())
    assert wr._RUN_LOCK.acquire(blocking=False), "测试需先占用 run 锁"
    try:
        wr.action_run("x", "x", "x", "pre-w-001")  # 必须抛 RunConflict
    finally:
        wr._RUN_LOCK.release()
    return True


def w15_upload_index_corrupted_rejected() -> bool:
    """上传索引损坏 → 显性拒绝（P4：不静默重置为空）。"""
    wr = _prepare_webapp(_tmpdir())
    wr._index_path().parent.mkdir(parents=True, exist_ok=True)
    wr._index_path().write_text("{ this is not json", encoding="utf-8")
    wr.action_upload("csv", "x.csv", b"a,b\n1,2\n")  # 必须抛 ValueError
    return True


# ---------------------------------------------------------------- 声明式用例表

CASES: tuple = (
    ("W1 独立端口 8766 与独立 CSRF", w1_independent_port_and_csrf),
    ("W2 上传 CSV 原样落盘+指纹", w2_upload_csv_original_bytes),
    ("W3 上传清单解析四要素", w3_upload_manifest_parsed),
    ("W4 预注册选项列表", w4_prereg_options_list),
    ("W5 端到端 run（GREEN/clean/复现包/Quarto/DP）", w5_run_e2e_research),
    ("W6 报告预览", w6_report_preview),
    ("W7 导出 Quarto [R]", w7_export_qmd),
    ("W8 导出复现包 zip", w8_export_repro_zip),
    ("W9 幂等键防重复", w9_idempotency_key),
    ("W10 历史列表", w10_history),
    ("W11 失败显性登记进事件流", w11_failure_registered_audit),
)

REJECT_CASES: tuple = (
    ("W12 未上传直接 run 拒绝", "ValueError", w12_run_without_upload_rejected),
    ("W13 清单缺要素拒绝", "DatasetGateError", w13_manifest_missing_element_rejected),
    ("W14 并发 run 拒绝", "RunConflict", w14_concurrent_run_rejected),
)

FAILMSG_CASES: tuple = (
    ("W15 上传索引损坏显性拒绝", w15_upload_index_corrupted_rejected, "损坏"),
)


def _import_exc(exc_name: str):
    import importlib

    for mod in ("plugins.research.webapp_r", "plugins.research.dataset_gate"):
        try:
            m = importlib.import_module(mod)
        except Exception:  # noqa: BLE001 — 模块未实现时保持 RED 而非收集崩溃
            continue
        if hasattr(m, exc_name):
            return getattr(m, exc_name)
    return RuntimeError  # 模块未实现 → 用例以 RuntimeError 兜底仍可执行（RED 期）


def _resolve(cases: tuple) -> tuple:
    out = []
    for name, exc_name, fn in cases:
        if exc_name == "ValueError":
            exc_type = ValueError
        else:
            exc_type = _import_exc(exc_name)
        out.append((name, exc_type, fn))
    return tuple(out)


# ---------------------------------------------------------------- HTTP 安全层（pytest）


def _start_srv(wr, tmp: Path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), wr.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, f"http://127.0.0.1:{srv.server_port}"


def test_w16_post_without_csrf_403(tmp_path, monkeypatch):
    """变更操作无 CSRF 令牌 → 403（P1-05 本机会话认证）。"""
    import plugins.research.webapp_r as wr
    wr.ROOT = tmp_path
    wr.UPLOAD_INDEX = tmp_path / "data" / "research" / "uploads" / "index.json"
    wr.UPLOAD_SHA_INDEX = tmp_path / "data" / "research" / "uploads" / "sha.json"
    wr._state = {"runs": [], "uploads": {}}
    srv, t, base = _start_srv(wr, tmp_path)
    try:
        req = urllib.request.Request(base + "/api/upload", data=b"x",
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("无 CSRF 令牌的 POST 必须被拒绝")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"应为 403，实为 {e.code}"
            body = json.loads(e.read())
            assert "CSRF" in body.get("error", ""), "必须点名 CSRF 令牌"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_w17_cross_origin_blocked(tmp_path, monkeypatch):
    """跨源 Origin → 403（DNS rebinding / CSRF 最小防护）。"""
    import plugins.research.webapp_r as wr
    wr.ROOT = tmp_path
    srv, t, base = _start_srv(wr, tmp_path)
    try:
        req = urllib.request.Request(base + "/api/history")
        req.add_header("Origin", "http://evil.example")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("跨源请求必须被拒绝")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"应为 403，实为 {e.code}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_w18_session_endpoint(tmp_path, monkeypatch):
    """GET /api/session 返回 CSRF 令牌（页面 JS 从这里取会话凭据）。"""
    import plugins.research.webapp_r as wr
    wr.ROOT = tmp_path
    srv, t, base = _start_srv(wr, tmp_path)
    try:
        with urllib.request.urlopen(base + "/api/session", timeout=10) as r:
            assert r.status == 200
            d = json.loads(r.read())
        assert d["csrf_token"] == wr._CSRF_TOKEN, "session 必须返回本实例令牌"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def test_w19_prereg_plan_enables_run_button():
    """浏览器：预注册计划异步加载完成后，运行按钮必须自动启用。"""
    pytest = __import__("pytest")
    pytest.importorskip("playwright", reason="未安装 playwright")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("未安装 playwright")

    base = "http://127.0.0.1:8766"
    try:
        urllib.request.urlopen(base + "/", timeout=2)
    except Exception:
        pytest.skip("face=R webapp 未启动")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(base, wait_until="load")
        root = ROOT / "examples"
        page.set_input_files("#file", str(root / "cohort_20260831.csv"))
        page.set_input_files("#file-man", str(root / "research_dataset_manifest.json"))
        page.set_input_files("#file-pre", str(root / "prereg_cohort_example.json"))
        page.wait_for_function("() => document.querySelector('#plan-select').options.length > 0",
                               timeout=15000)
        page.wait_for_timeout(100)
        assert page.locator("#btn-run").is_enabled(), (
            "预注册计划加载完成后运行按钮仍为 disabled"
        )
        browser.close()


def test_w20_frontend_rechecks_ready_after_plan_load():
    """前端契约：计划异步加载后必须重新检查运行按钮可用状态。"""
    source = (ROOT / "plugins" / "research" / "webapp_r.py").read_text(
        encoding="utf-8"
    )
    start = source.index("function loadPlans(preregId)")
    end = source.index("function ready()", start)
    load_plans = source[start:end]
    enabled = load_plans.index("sel.disabled=false;")
    assert "ready();" in load_plans[enabled:], (
        "loadPlans 异步启用计划选择器后必须调用 ready()，否则运行按钮会一直禁用"
    )


def test_w21_research_intake_explains_three_files_and_csv_schema():
    """论文轨首页必须说明三类材料用途，并明确队列 CSV 的最小字段。"""
    source = (ROOT / "plugins" / "research" / "webapp_r.py").read_text(
        encoding="utf-8"
    )
    for text in (
        "每行一位参与者",
        "不是订单 CSV",
        "participant_id",
        "arm（a/b）",
        "converted（0/1）",
        "数据从哪里来",
        "预先登记",
        "防止事后挑结果",
    ):
        assert text in source, f"论文轨首页缺少面向新手的说明：{text}"


def test_research_csv_requires_minimum_fields():
    """研究队列 CSV 必须具备 participant_id/arm/converted 三个最小字段。"""
    import plugins.research.webapp_r as wr

    with pytest.raises(ValueError, match="participant_id.*arm.*converted"):
        wr._validate_csv_bytes(b"participant_id,arm\np-001,a\n")


def test_research_csv_rejects_commerce_order_csv_explicitly():
    """订单 CSV 误传到论文轨时，错误必须明确指出轨道和文件类型。"""
    import plugins.research.webapp_r as wr

    order_csv = "订单编号,created_at,销售渠道,amount_total,amount_paid,status\n"
    order_csv += "SO-1001,2026-08-26,线上商城,42.00,42.00,paid\n"
    with pytest.raises(ValueError, match="订单 CSV.*研究轨"):
        wr._validate_csv_bytes(order_csv.encode("utf-8"))


def test_research_report_has_plain_language_preview_payload():
    """报告接口应提供给非统计用户看的中文结果预览。"""
    import plugins.research.webapp_r as wr

    preview = wr._plain_report_preview({
        "conclusion_status": "GREEN",
        "metric": "conversion_rate",
        "counts": [325, 650, 217, 650],
        "reconcile": {"reconcile_diff": "clean"},
        "prereg_id": "pre-cohort-20260831",
    })
    assert preview["title"] == "研究队列转化率对比"
    assert "325/650" in preview["summary"]
    assert "217/650" in preview["summary"]
    assert "百分点" in preview["summary"]
    assert preview["status_explanation"]


def test_research_page_contains_report_preview_module():
    """研究轨报告页必须渲染结果预览，而不是只显示内部步骤和英文方法名。"""
    import plugins.research.webapp_r as wr

    for text in (
        'id="rp-preview"',
        'id="rp-preview-title"',
        'id="rp-preview-summary"',
        'id="rp-preview-explanation"',
        "结果预览",
        "GREEN 表示",
    ):
        assert text in wr.PAGE, f"研究轨报告页缺少预览内容：{text}"
    assert "rp-preview-summary" in wr.PAGE[wr.PAGE.index("function renderReport"):]


def test_research_report_preview_html_uses_run_facts():
    """报告预览 HTML 必须来自运行台账，并包含可读结论和实际数字。"""
    import plugins.research.webapp_r as wr

    html = wr._render_report_html({
        "run_id": "r-test",
        "conclusion_status": "GREEN",
        "metric": "conversion_rate",
        "counts": [325, 650, 217, 650],
        "reconcile": {"reconcile_diff": "clean"},
        "prereg_id": "pre-test",
    })
    assert "研究结论报告" in html
    assert "325/650" in html and "217/650" in html
    assert "16.62" in html
    assert "r-test" in html


def test_research_page_has_two_distinct_preview_views():
    """报告页必须区分结果预览与最终报告预览。"""
    import plugins.research.webapp_r as wr

    for text in (
        'id="tab-result-preview"',
        'id="tab-report-preview"',
        'id="rp-result-preview"',
        'id="rp-report-preview"',
        'id="rp-report-frame"',
        "/api/report/html?run=",
        "function switchPreviewTab",
        "结果预览",
        "报告预览",
    ):
        assert text in wr.PAGE, f"研究轨报告页缺少双预览元素：{text}"


if __name__ == "__main__":
    sys.exit(casekit.run_cli("W face=R 独立入口测试",
                             CASES, _resolve(REJECT_CASES), FAILMSG_CASES))

test_pass, test_reject, test_failmsg = casekit.pytest_cases(
    CASES, _resolve(REJECT_CASES), FAILMSG_CASES)
