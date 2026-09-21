#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""face=A 控制面测试（ADR-15 三面入口 · S4 双轨合龙）。

  A1 登录未配置 → not_configured（显性区分「没配」与「配了连不上」）
  A2 登录成功 → 15 分钟会话 sid
  A3 错误令牌 → 拒绝 + 审计 login_failed
  A4 会话过期 → 拒绝（ADR-15 不静默续期）
  A5 双轨健康总览：双轨结构 + 台账聚合（含断言摘要）
  A6 部署 diff 预览：overlay 差异 + 对照册来源
  A7 导出桥无二次确认 → 拒绝（P1-04：人审 + confirm 双重门槛）
  A8 导出桥全流程成功：双端事件 + manifest + sha 一致
  A9 非法导出方向 → 拒绝
  A10 HTTP 无 CSRF → 403
  A11 HTTP 跨源 Origin → 403
  A12 HTTP 未登录访问 /api/health → 401

运行：仓库根目录下  pytest -q tests/test_admin_webapp.py
"""
import hashlib
import json
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import plugins.admin.webapp_a as wa
from core.audit import ledger
from core.ingestion.context import TrackContext


@pytest.fixture()
def admin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """构造双轨卷 + 源台账 run + 快照 + 控制面临时卷，patch 模块常量。"""
    c_vol = tmp_path / "v_commerce"
    r_vol = tmp_path / "v_research"
    bridge = tmp_path / "bridge"
    admin_vol = tmp_path / "admin"
    for v in (c_vol, r_vol, bridge, admin_vol):
        v.mkdir(parents=True, exist_ok=True)

    # 源轨（commerce）：一条真实 run + 快照
    src_ctx = TrackContext(track="commerce", volume_root=c_vol)
    snap_dir = src_ctx.snapshots_dir
    snap_dir.mkdir(parents=True)
    snap = snap_dir / "s-admin-0001"
    snap.write_bytes(b"ADMIN-SNAPSHOT-" + b"\x00" * 96)
    snap_sha = hashlib.sha256(snap.read_bytes()).hexdigest()
    ledger.write_run(c_vol / "ledger.db", {
        "run_id": "r-admin-0001", "track": "commerce",
        "started_at": "2026-08-31T11:00:00", "batch_id": "b-admin-001",
        "snapshot_id": "s-admin-0001", "snapshot_sha256": snap_sha,
        "rows": 8, "metric": "pay_success_rate", "metric_version": 1,
        "health_summary": {"rows": 8},
        "provenance": {"run_id": "r-admin-0001", "snapshot_id": "s-admin-0001",
                       "batch_id": "b-admin-001", "commit_sha": "0" * 64,
                       "snapshot_sha256": snap_sha},
        "steps": ["ok"],
    })
    # 断言摘要（最近 run 的 JSON 台账）
    (c_vol / "runs").mkdir(parents=True)
    (c_vol / "runs" / "r-admin-0001.json").write_text(json.dumps({
        "run_id": "r-admin-0001",
        "assertion_report": {"assertions": [
            {"name": "row_count", "passed": True},
            {"name": "unique_order", "passed": True}]},
    }), encoding="utf-8")

    # 目标轨（research）：空卷台账
    ledger.write_run(r_vol / "ledger.db", {
        "run_id": "r-res-0001", "track": "research",
        "started_at": "2026-08-31T11:05:00", "batch_id": "b-res-001",
        "snapshot_id": "s-res-0001", "snapshot_sha256": "0" * 64,
        "rows": 100, "metric": "conversion_rate", "metric_version": 1,
        "health_summary": {"rows": 100},
        "provenance": {"run_id": "r-res-0001", "snapshot_id": "s-res-0001",
                       "batch_id": "b-res-001", "commit_sha": "0" * 64},
        "steps": ["ok"],
    })

    monkeypatch.setattr(wa, "COMMERCE_VOLUME", c_vol)
    monkeypatch.setattr(wa, "RESEARCH_VOLUME", r_vol)
    monkeypatch.setattr(wa, "BRIDGE_DIR", bridge)
    monkeypatch.setattr(wa, "ADMIN_VOLUME", admin_vol)
    monkeypatch.setenv("TRACEABLE_ADMIN_TOKEN", "admin-secret-token")
    wa._SESSIONS.clear()
    return {"src_ctx": src_ctx, "snap_sha": snap_sha}


def _login() -> str:
    out = wa.action_login("admin-secret-token")
    assert out["ok"], out
    return out["sid"]


# ---------------------------------------------------------------- 会话

def test_a1_login_not_configured(admin_env, monkeypatch):
    """A1：未配置管理令牌 → not_configured（显性，与「配了连不上」区分）。"""
    monkeypatch.delenv("TRACEABLE_ADMIN_TOKEN", raising=False)
    out = wa.action_login("anything")
    assert out["ok"] is False and out["reason"] == "not_configured"


def test_a2_login_success(admin_env):
    """A2：正确令牌 → ok + sid + 15 分钟 TTL。"""
    out = wa.action_login("admin-secret-token")
    assert out["ok"] and out["sid"] and out["expires_in"] == 15 * 60
    assert wa._session_ok(out["sid"])


def test_a3_login_wrong_token(admin_env):
    """A3：错误令牌 → 拒绝，且 login_failed 进 admin 审计流（D8）。"""
    out = wa.action_login("wrong")
    assert out["ok"] is False and out["reason"] == "rejected"
    conn = sqlite3.connect(str(wa.ADMIN_VOLUME / "ledger.db"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM events "
                         "WHERE event_type='login_failed'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "登录失败必须进 append-only 审计流"


def test_a4_session_expired(admin_env):
    """A4：会话 15 分钟过期 → 拒绝（不静默续期）。"""
    sid = _login()
    with wa._SESSION_LOCK:
        wa._SESSIONS[sid] = time.monotonic() - 1   # 已过期
    with pytest.raises(wa.AdminError, match="过期"):
        wa.action_health(sid)
    assert not wa._session_ok(sid), "过期会话必须被清除"


# ---------------------------------------------------------------- 健康总览 + diff

def test_a5_health_overview(admin_env):
    """A5：双轨健康总览：双轨结构 + runs 计数 + 断言摘要 + bridge 事件计数。"""
    sid = _login()
    h = wa.action_health(sid)
    assert len(h["tracks"]) == 2
    c = next(t for t in h["tracks"] if t["track"] == "commerce")
    r = next(t for t in h["tracks"] if t["track"] == "research")
    assert c["runs"] == 1 and c["failures"] == 0
    assert c["last_run"]["run_id"] == "r-admin-0001"
    assert c["last_assertions"] == {"assertions": 2, "failed": 0}
    assert r["runs"] == 1
    assert h["bridge"]["export_events"] == 0
    # 无会话 → 拒绝
    with pytest.raises(wa.AdminError, match="会话"):
        wa.action_health("bogus-sid")


def test_a6_deploy_diff_preview(admin_env):
    """A6：部署 diff 预览：overlay 差异 + 对照册来源（ADR-16）。"""
    sid = _login()
    d = wa.action_deploy_diff(sid)
    assert d["commerce"]["track"] == "commerce"
    assert "app.session_timeout_minutes" in d["commerce"]["changed"]
    assert d["research"]["changed"]["research.k_anonymity_default"] == {
        "before": 2, "after": 5}
    led = d["ledger"]["ledger"]
    assert led["research.k_anonymity_default"]["source"] == "overlay.research"
    assert led["app.session_timeout_minutes"]["source"] == "overlay.commerce"
    assert led["shared.audit_retention_days"]["source"] == "base"


# ---------------------------------------------------------------- 导出桥人审触发

def test_a7_bridge_needs_confirmation(admin_env):
    """A7：无二次确认 → 拒绝（P1-04：导出桥只能由人审控制面触发）。"""
    sid = _login()
    with pytest.raises(wa.AdminError, match="二次确认"):
        wa.action_bridge_export(sid, "commerce→research", "r-admin-0001",
                                confirm=False)


def test_a8_bridge_export_success(admin_env):
    """A8：导出桥全流程：confirm=true + 会话 → 五闸导出成功 + 双端事件。"""
    sid = _login()
    out = wa.action_bridge_export(sid, "commerce→research", "r-admin-0001",
                                  confirm=True)
    assert out["ok"] and out["export_id"]
    assert out["manifest"]["source"]["track"] == "commerce"
    imported = Path(out["imported"])
    assert hashlib.sha256(imported.read_bytes()).hexdigest() == admin_env["snap_sha"]
    # 双端审计（core/bridge 闸5）+ 控制面审计
    for db in (wa.COMMERCE_VOLUME / "ledger.db", wa.RESEARCH_VOLUME / "ledger.db"):
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM events "
                             "WHERE event_type='export_bridge'").fetchone()[0]
        finally:
            conn.close()
        assert n >= 1, f"导出桥必须双端审计落账: {db}"
    conn = sqlite3.connect(str(wa.ADMIN_VOLUME / "ledger.db"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM events "
                         "WHERE event_type='bridge_export'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "控制面触发动作必须进 admin 审计流"


def test_a9_bridge_invalid_direction(admin_env):
    """A9：非法导出方向 → 拒绝（不静默）。"""
    sid = _login()
    with pytest.raises(wa.AdminError, match="非法导出方向"):
        wa.action_bridge_export(sid, "commerce→mars", "r-admin-0001",
                                confirm=True)


# ---------------------------------------------------------------- HTTP 层

def _start_server():
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), wa.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _req(base: str, path: str, payload: dict | None = None,
         csrf: str | None = wa._CSRF_TOKEN, origin: str | None = None,
         method: str = "POST"):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if csrf is not None:
        req.add_header("X-CSRF-Token", csrf)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_a10_http_csrf_required(admin_env):
    """A10：HTTP 变更请求无 CSRF → 403。"""
    srv, base = _start_server()
    try:
        code, _ = _req(base, "/api/login", {"token": "admin-secret-token"},
                       csrf=None)
        assert code == 403
    finally:
        srv.shutdown()


def test_a11_http_cross_origin_blocked(admin_env):
    """A11：跨源 Origin → 403（P1-05 CSRF/Origin 防护）。"""
    srv, base = _start_server()
    try:
        code, _ = _req(base, "/api/login", {"token": "admin-secret-token"},
                       origin="http://evil.example.com")
        assert code == 403
    finally:
        srv.shutdown()


def test_a12_http_unauthenticated_health(admin_env):
    """A12：未登录（伪 sid）访问 /api/health → 401（会话闸门）。"""
    srv, base = _start_server()
    try:
        code, body = _req(base, "/api/health", {"sid": "bogus-sid"})
        assert code == 401
        assert "会话" in body.get("error", "")
    finally:
        srv.shutdown()
