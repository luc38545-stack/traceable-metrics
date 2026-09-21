"""face=A 控制面（ADR-15 三面独立入口 · S4 双轨合龙）。

职责（对照架构书/审计意见）：
- 独立 URL 与登录态：端口 8767（face=C=8765 / face=R=8766），独立 CSRF 令牌；
- 15 分钟会话超时（ADR-15）：登录后会话 15 分钟过期，过期即 401（不静默续期）；
- 双轨健康总览（S4）：聚合 commerce/research 两侧台账（只读），双轨状态一览；
- 部署 diff 预览（S4 + ADR-16）：base.yml + 双轨 overlay 合并差异 + 对照册；
- 导出桥人审触发（P1-04：导出桥只能由人审控制面触发）：
  登录会话 + 二次确认（confirm=true）+ CSRF 三重门槛，
  内部完成「签发一次性 token → 五闸导出」（core/bridge 双端审计）。

数据卷路径均为 app 层常量（C-4 约束 core/，不约束插件层）：
  COMMERCE_VOLUME / RESEARCH_VOLUME / BRIDGE_DIR / ADMIN_VOLUME
管理令牌经 TRACEABLE_ADMIN_TOKEN env 注入（用户自填，不落盘）。

用法：python -m plugins.admin.webapp_a   （127.0.0.1:8767）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH

PORT = int(os.environ.get("TRACEABLE_ADMIN_PORT", "8767"))
SESSION_TTL = 15 * 60  # ADR-15：15 分钟会话

COMMERCE_VOLUME = ROOT / "data" / "commerce"
RESEARCH_VOLUME = ROOT / "data" / "research"
BRIDGE_DIR = ROOT / "data" / "_bridge"
ADMIN_VOLUME = ROOT / "data" / "admin"

_CSRF_TOKEN = secrets.token_hex(16)
_SESSIONS: dict[str, float] = {}          # sid -> 过期时间戳（单调时间）
_SESSION_LOCK = threading.Lock()
_RATE: dict[str, list[float]] = {}
_RATE_WINDOW, _RATE_MAX = 60.0, 120


class AdminError(RuntimeError):
    """控制面操作被拒（显性，不静默）。"""


def _safe_error(e: Exception) -> str:
    msg = str(e) or type(e).__name__
    msg = msg.replace(str(ROOT), "<repo>")
    msg = re.sub(r"[A-Za-z]:[\\/][^\s'\"]{1,120}", "<path>", msg)
    return (msg or "内部错误")[:400]


# ---------------------------------------------------------------- 会话（ADR-15）

def _admin_token() -> str:
    return os.environ.get("TRACEABLE_ADMIN_TOKEN", "")


def action_login(token: str) -> dict:
    """登录：TRACEABLE_ADMIN_TOKEN 未配置 → not_configured（显性区分「没配」）。"""
    want = _admin_token()
    if not want:
        return {"ok": False, "reason": "not_configured",
                "detail": "未配置 TRACEABLE_ADMIN_TOKEN（控制面登录未启用）"}
    if not secrets.compare_digest(token or "", want):
        _audit("login_failed", subject_id=None, payload={"reason": "wrong token"})
        return {"ok": False, "reason": "rejected", "detail": "令牌错误"}
    sid = f"a-{secrets.token_hex(12)}"
    with _SESSION_LOCK:
        _SESSIONS[sid] = time.monotonic() + SESSION_TTL
    _audit("login", subject_id=sid, payload={"ttl_seconds": SESSION_TTL})
    return {"ok": True, "sid": sid, "expires_in": SESSION_TTL}


def _session_ok(sid: str) -> bool:
    """15 分钟会话校验；过期即删除并拒绝（ADR-15：不静默续期）。"""
    if not sid:
        return False
    with _SESSION_LOCK:
        exp = _SESSIONS.get(sid)
        if exp is None:
            return False
        if time.monotonic() >= exp:
            _SESSIONS.pop(sid, None)
            return False
        return True


def _audit(event_type: str, subject_id: str | None, payload: dict) -> None:
    """控制面动作进 admin 台账 append-only 事件流（D8：登录等全覆盖）。"""
    from core.audit import ledger as _ledger

    ADMIN_VOLUME.mkdir(parents=True, exist_ok=True)
    _ledger.log_event(ADMIN_VOLUME / "ledger.db", event_type,
                      subject_id=subject_id, track="admin", payload=payload)


# ---------------------------------------------------------------- 双轨健康总览（S4）

def _track_summary(track: str, volume: Path) -> dict:
    """单轨健康摘要：台账只读聚合（无台账 → 如实「无运行记录」）。"""
    ledger_db = volume / "ledger.db"
    summary = {"track": track, "ledger_exists": ledger_db.exists(),
               "runs": 0, "failures": 0, "last_run": None, "recent_events": []}
    if not ledger_db.exists():
        summary["state"] = "no_runs"
        return summary
    try:
        conn = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True)
        try:
            summary["runs"] = conn.execute(
                "SELECT COUNT(*) FROM runs").fetchone()[0]
            summary["failures"] = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='run_failed'"
                ).fetchone()[0]
            row = conn.execute(
                "SELECT run_id, started_at, metric, snapshot_id FROM runs "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
            if row:
                summary["last_run"] = {
                    "run_id": row[0], "started_at": row[1],
                    "metric": row[2], "snapshot_id": row[3]}
            ev = conn.execute(
                "SELECT event_type, ts, payload FROM events "
                "ORDER BY ts DESC LIMIT 5").fetchall()
            summary["recent_events"] = [
                {"event_type": r[0], "ts": r[1], "payload": json.loads(r[2])}
                for r in ev]
        finally:
            conn.close()
    except sqlite3.Error as e:
        summary["state"] = "ledger_unreadable"
        summary["detail"] = _safe_error(e)
        return summary
    # 最近 run 的断言闸门摘要（从 runs/r-*.json 读，无则如实标注）
    try:
        runs_dir = volume / "runs"
        if runs_dir.exists():
            latest = sorted(runs_dir.glob("r-*.json"),
                            key=lambda p: p.stat().st_mtime)
            if latest:
                rec = json.loads(latest[-1].read_text(encoding="utf-8"))
                ar = rec.get("assertion_report")
                if ar:
                    summary["last_assertions"] = {
                        "assertions": len(ar.get("assertions", [])),
                        "failed": sum(
                            1 for a in ar.get("assertions", []) if not a["passed"]),
                    }
    except Exception:  # noqa: BLE001 — 断言摘要读不到不影响健康总览主体
        summary["last_assertions"] = {"note": "unreadable"}
    summary["state"] = "ok"
    return summary


def action_health(sid: str) -> dict:
    """双轨健康总览（需有效会话）。"""
    if not _session_ok(sid):
        raise AdminError("会话无效或已过期（ADR-15 15 分钟），请重新登录")
    c = _track_summary("commerce", COMMERCE_VOLUME)
    r = _track_summary("research", RESEARCH_VOLUME)
    bridge_events = 0
    for db in (COMMERCE_VOLUME / "ledger.db", RESEARCH_VOLUME / "ledger.db"):
        if db.exists():
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    bridge_events += conn.execute(
                        "SELECT COUNT(*) FROM events WHERE event_type='export_bridge'"
                        ).fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
    return {"tracks": [c, r], "bridge": {"export_events": bridge_events},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


# ---------------------------------------------------------------- 部署 diff 预览（ADR-16）

def action_deploy_diff(sid: str) -> dict:
    """部署 diff 预览：base + 双轨 overlay 合并差异 + 对照册（需有效会话）。"""
    if not _session_ok(sid):
        raise AdminError("会话无效或已过期（ADR-15 15 分钟），请重新登录")
    from core.config.overlay import (
        build_ledger,
        load_overlay_dir,
        render_diff_preview,
    )

    data = load_overlay_dir(ROOT / "infra" / "config")
    return {
        "commerce": render_diff_preview(
            data["base"], data["commerce_overlay"], "commerce"),
        "research": render_diff_preview(
            data["base"], data["research_overlay"], "research"),
        "ledger": build_ledger(data["base"], data["commerce_overlay"],
                               data["research_overlay"]),
    }


# ---------------------------------------------------------------- 导出桥人审触发（S4）

def action_bridge_export(sid: str, purpose: str, run_id: str,
                         confirm: bool = False) -> dict:
    """导出桥人审触发：会话 + 二次确认（confirm=true）+ 五闸导出。

    内部一次性完成「签发 token → 立即消费」：人审批准 = 有效会话 + confirm，
    core/bridge 的五闸（含 token 一次性/过期防线）仍逐闸生效。
    """
    if not _session_ok(sid):
        raise AdminError("会话无效或已过期（ADR-15 15 分钟），请重新登录")
    if not confirm:
        raise AdminError("导出桥需要二次确认（confirm=true）——跨轨数据流动是敏感操作")
    if purpose not in ("commerce→research", "research→commerce"):
        raise AdminError(f"非法导出方向: {purpose!r}（仅 commerce→research / "
                         "research→commerce）")
    if not run_id:
        raise AdminError("必须指定要导出的源 run_id")

    from core.bridge import export_snapshot, issue_transfer_token
    from core.ingestion.context import TrackContext

    source_track, target_track = purpose.split("→")
    src_ctx = TrackContext(track=source_track,
                           volume_root=(COMMERCE_VOLUME if source_track == "commerce"
                                        else RESEARCH_VOLUME))
    tgt_ctx = TrackContext(track=target_track,
                           volume_root=(COMMERCE_VOLUME if target_track == "commerce"
                                        else RESEARCH_VOLUME))
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    tok = issue_transfer_token(BRIDGE_DIR, purpose=purpose,
                               source_track=source_track,
                               target_track=target_track)
    try:
        out = export_snapshot(
            token_id=tok.token_id, purpose=purpose,
            source_ctx=src_ctx, target_ctx=tgt_ctx,
            bridge_dir=BRIDGE_DIR,
            source_ledger_db=src_ctx.volume_root / "ledger.db",
            target_ledger_db=tgt_ctx.volume_root / "ledger.db",
            run_id=run_id)
    except Exception as e:  # noqa: BLE001 — 导出失败也要留审计（P4 显性）
        _audit("bridge_export_failed", subject_id=run_id,
               payload={"purpose": purpose, "error": _safe_error(e)})
        raise
    _audit("bridge_export", subject_id=out["export_id"],
           payload={"purpose": purpose, "run_id": run_id,
                    "sha256": out["sha256"]})
    return {"ok": True, "export_id": out["export_id"],
            "manifest": out["manifest"], "imported": out["imported"]}


# ---------------------------------------------------------------- HTTP 层

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>TraceableMetrics · 控制面 (face=A)</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem;background:#111;color:#eee}
code{background:#222;padding:.1em .4em;border-radius:4px}
button{padding:.5em 1em;margin:.2em;cursor:pointer}</style></head>
<body>
<h1>TraceableMetrics 控制面（face=A · 15 分钟会话）</h1>
<div id="login">
  <p>管理令牌 <input id="tok" type="password" size="40"></p>
  <button onclick="login()">登录</button><span id="msg"></span>
</div>
<div id="panel" style="display:none">
  <h2>双轨健康总览</h2><pre id="health" style="white-space:pre-wrap"></pre>
  <h2>部署 diff 预览（ADR-16 overlay）</h2><pre id="diff" style="white-space:pre-wrap"></pre>
  <h2>导出桥（人审触发）</h2>
  <p>方向 <select id="purpose">
    <option>commerce→research</option><option>research→commerce</option>
  </select> 源 run_id <input id="runid" size="20"></p>
  <button onclick="bridge()">二次确认：触发导出桥</button><pre id="br" style="white-space:pre-wrap"></pre>
</div>
<script>
var CSRF="__CSRF_TOKEN__", SID=null;
function j(p,o){return fetch(p,{method:'POST',headers:{'X-CSRF-Token':CSRF,'Content-Type':'application/json'},body:JSON.stringify(o||{})}).then(function(r){return r.json()})}
function login(){j('/api/login',{token:document.getElementById('tok').value}).then(function(d){
  if(d.ok){SID=d.sid;document.getElementById('login').style.display='none';
    document.getElementById('panel').style.display='block';
    document.getElementById('msg').textContent='已登录 '+d.expires_in+' 秒';
    health();diff();}else{document.getElementById('msg').textContent=d.reason+':'+d.detail}}) }
function health(){j('/api/health',{sid:SID}).then(function(d){document.getElementById('health').textContent=JSON.stringify(d,null,2)})}
function diff(){j('/api/deploy/diff',{sid:SID}).then(function(d){document.getElementById('diff').textContent=JSON.stringify(d,null,2)})}
function bridge(){j('/api/bridge/export',{sid:SID,purpose:document.getElementById('purpose').value,
  run_id:document.getElementById('runid').value,confirm:true}).then(function(d){
  document.getElementById('br').textContent=JSON.stringify(d,null,2);health();})}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(30)
        except Exception:  # noqa: BLE001
            pass

    def _rate_limited(self) -> bool:
        ip = self.client_address[0]
        now = time.monotonic()
        q = _RATE.setdefault(ip, [])
        while q and now - q[0] > _RATE_WINDOW:
            q.pop(0)
        q.append(now)
        if len(q) > _RATE_MAX:
            self._json({"error": "请求过于频繁，请稍后重试"}, 429)
            return True
        return False

    def _reject_cross_origin(self) -> bool:
        host = self.headers.get("Host", "")
        if host and host.split(":", 1)[0] not in ("127.0.0.1", "localhost"):
            self._json({"error": "forbidden host"}, 403)
            return True
        origin = self.headers.get("Origin")
        if origin:
            allow = (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")
            if origin not in allow:
                self._json({"error": "cross-origin request blocked"}, 403)
                return True
        return False

    def _csrf_ok(self) -> bool:
        if self.headers.get("X-CSRF-Token") != _CSRF_TOKEN:
            self._json({"error": "missing or invalid CSRF token（本机会话令牌）"}, 403)
            return False
        return True

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self) -> None:
        body = PAGE.replace("__CSRF_TOKEN__", _CSRF_TOKEN).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length > 1 << 20:
            raise AdminError("请求体过大")
        body = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(body or b"{}")
        except Exception as e:  # noqa: BLE001
            raise AdminError(f"请求体不是合法 JSON: {e}") from e

    def do_GET(self) -> None:
        if self._reject_cross_origin() or self._rate_limited():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._page()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self._reject_cross_origin() or self._rate_limited():
            return
        if not self._csrf_ok():
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/login":
                self._json(action_login(body.get("token", "")))
                return
            sid = body.get("sid", "")
            if path == "/api/health":
                self._json(action_health(sid))
                return
            if path == "/api/deploy/diff":
                self._json(action_deploy_diff(sid))
                return
            if path == "/api/bridge/export":
                self._json(action_bridge_export(
                    sid, body.get("purpose", ""), body.get("run_id", ""),
                    confirm=bool(body.get("confirm"))))
                return
        except AdminError as e:
            self._json({"error": _safe_error(e)}, 401 if "会话" in str(e) else 400)
            return
        except Exception as e:  # noqa: BLE001
            self._json({"error": _safe_error(e)}, 400)
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    # 本机全局代理坑：进程内清掉代理变量，保证 127.0.0.1 直连（见项目记忆）
    for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY",
              "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,<-loopback>"
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"TraceableMetrics face=A 控制面： http://127.0.0.1:{PORT} （CSRF 启动令牌已生成）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
