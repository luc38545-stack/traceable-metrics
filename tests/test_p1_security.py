#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 隔离 / 授权 / 运行安全回归测试（整改意见 P1-03~P1-08）。

每条用例都对应整改文档「必增测试」点名的一条，且满足文档第 4 条铁律：
**在旧实现上必须失败**。它们各自盯住的旧缺陷是——

- P1-03：上传索引保存任意绝对路径；索引损坏时静默重置为空；
        条目不记录 SHA/大小/时间；checkup 用前不核验文件指纹（TOCTOU）。
- P1-04：key 只绑动作×轨道，没有 project/有效期/撤销；L3×research 未强制禁用；
        非法轨道注册不拒绝。
- P1-05：POST 无 CSRF/启动令牌校验；错误响应会把内部绝对路径返回前端。
- P1-06：compile_metric 仍用 `" AND ".join(filters)` 拼接裸 SQL；filter 字段
        不校验是否在模型 schema 内；取数端不校验「已编译登记」；含 test_order
        列的数据集不会自动排除测试单（架构书清单 4-A）。
- P1-07：缺少「并发运行一条成功一条 409」的回归用例（实现已就位，补测试）。
- P1-08：multipart 解析必须逐字节保真（旧 strip 吃掉首尾合法换行）。

运行：仓库根目录下  pytest -q tests/test_p1_security.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH

from core.auth import tool_registry
from core.auth.tool_registry import PermissionDenied, ToolRegistry, register_key, revoke_key
from core.semantic.compiler import ContractError, compile_metric, load_contract
from core.semantic.api import SemanticQueryError, query_metric

import plugins.commerce.webapp as webapp  # noqa: E402

# 本机回环一律直连：环境里若设了 http_proxy，urllib 会把 127.0.0.1 也发给代理。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)


# ---------------------------------------------------------------- HTTP 夹具

@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """一个隔离的 webapp 实例：数据目录全部指到 tmp_path，绝不污染仓库 data/。"""
    monkeypatch.setattr(webapp, "ROOT", tmp_path)
    up = tmp_path / "data" / "commerce" / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webapp, "UPLOAD_INDEX", up / "index.json")
    monkeypatch.setattr(webapp, "UPLOAD_SHA_INDEX", up / "sha.json")
    monkeypatch.setattr(webapp, "_state", {"runs": [], "uploads": {}})
    webapp._IDEMPOTENCY.clear()
    webapp._RATE.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield SimpleNamespace(base=base, token=webapp._CSRF_TOKEN, uploads=up)
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return e.code, {}


def _post(base: str, path: str, token: str | None, data: bytes = b"",
          headers: dict | None = None) -> tuple[int, dict]:
    h = {}
    if token is not None:
        h["X-CSRF-Token"] = token
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return e.code, {}


def _multipart(file_bytes: bytes, name: str = "t.csv") -> tuple[bytes, str]:
    boundary = "testboundary"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{name}\"\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + file_bytes + tail, f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------- P1-03

def test_p103_index_stores_relative_names_only(srv):
    """索引只保存相对文件名（不可猜测对象 ID），不保存任意绝对路径。"""
    body, ctype = _multipart(b"order_id,status\r\n1,paid\r\n")
    st, d = _post(srv.base, "/api/upload", srv.token, data=body,
                  headers={"Content-Type": ctype})
    assert st == 200, d
    idx = json.loads(webapp.UPLOAD_INDEX.read_text(encoding="utf-8"))
    ent = idx[d["upload_id"]]
    name = ent["file"] if isinstance(ent, dict) else ent
    assert "/" not in name and "\\" not in name, f"索引出现路径分隔符：{name!r}"
    assert not re.match(r"^[A-Za-z]:", name), f"索引出现盘符绝对路径：{name!r}"
    assert Path(webapp.UPLOAD_INDEX.parent, name).exists()


def test_p103_corrupt_index_fails_loudly(srv, tmp_path):
    """索引损坏 → 显性拒绝，禁止静默重置为空（P4）。"""
    webapp.UPLOAD_INDEX.write_text("{ this is not json", encoding="utf-8")
    body, ctype = _multipart(b"order_id,status\r\n1,paid\r\n")
    st, d = _post(srv.base, "/api/upload", srv.token, data=body,
                  headers={"Content-Type": ctype})
    assert st == 400, f"损坏索引必须拒绝新上传（不得静默重建），got {st}"
    assert "索引" in d.get("error", ""), d
    # 索引内容保持损坏原样，没有被悄悄重置
    assert webapp.UPLOAD_INDEX.read_text(encoding="utf-8") == "{ this is not json"


def test_p103_entry_records_sha_size_time(srv):
    """上传条目记录 SHA-256、大小、创建时间（P1-03 第 4 条）。"""
    raw = b"order_id,status\r\n1,paid\r\n"
    body, ctype = _multipart(raw)
    st, d = _post(srv.base, "/api/upload", srv.token, data=body,
                  headers={"Content-Type": ctype})
    assert st == 200, d
    idx = json.loads(webapp.UPLOAD_INDEX.read_text(encoding="utf-8"))
    ent = idx[d["upload_id"]]
    assert ent["sha256"] == hashlib.sha256(raw).hexdigest()
    assert ent["size"] == len(raw)
    assert ent.get("created_at")


def test_p103_checkup_detects_file_tamper(srv):
    """checkup 使用前再次核验文件 SHA-256，磁盘文件被替换 → 显性失败（TOCTOU）。"""
    raw = b"order_id,status\r\n1,paid\r\n2,cancelled\r\n"
    body, ctype = _multipart(raw)
    st, d = _post(srv.base, "/api/upload", srv.token, data=body,
                  headers={"Content-Type": ctype})
    assert st == 200, d
    uid = d["upload_id"]
    idx = json.loads(webapp.UPLOAD_INDEX.read_text(encoding="utf-8"))
    fpath = webapp.UPLOAD_INDEX.parent / idx[uid]["file"]
    fpath.write_bytes(fpath.read_bytes() + b"\n1,tampered\n")   # 替换字节
    st, d = _get(srv.base, "/api/checkup?id=" + urllib.parse.quote(uid))
    assert st == 400, f"文件被替换后 checkup 必须失败，got {st}"
    assert "指纹" in d.get("error", ""), d


# ---------------------------------------------------------------- P1-04

def test_p104_illegal_track_rejected_at_register():
    """key 注册时 track 只能是 commerce|research，非法值先拒绝（P1-04 ②）。"""
    with pytest.raises(ValueError):
        register_key("k-bad-track", {"read_via_semantic"}, tracks={"commerce2"})


def test_p104_l3_denied_on_research():
    """L3 在 research 强制禁用（P1-04 ③）——即使 key 白名单包含 research。"""
    register_key("k-l3", {"read_via_semantic", "pipeline_rerun"},
                 tracks={"commerce", "research"}, agent_level="L3")
    reg = ToolRegistry()
    with pytest.raises(PermissionDenied):
        reg.authorize("L3", "read_via_semantic", "research", key_id="k-l3")
    # 商用轨 L3 不受影响（默认挂起语义之外仍按白名单放行）
    assert reg.authorize("L3", "read_via_semantic", "commerce",
                         key_id="k-l3").allowed


def test_p104_project_binding_enforced():
    """key 声明了 allowed_project_ids → 只认这些 project（P1-04 ①）。"""
    register_key("k-proj", {"read_via_semantic"}, tracks={"commerce"},
                 project_ids={"proj-a"})
    reg = ToolRegistry()
    with pytest.raises(PermissionDenied):
        reg.authorize("L2", "read_via_semantic", "commerce",
                      key_id="k-proj", project_id="proj-b")
    assert reg.authorize("L2", "read_via_semantic", "commerce",
                         key_id="k-proj", project_id="proj-a").allowed
    # 声明了 project_ids 却不带 project_id → 无法证明作用域 → 拒绝
    with pytest.raises(PermissionDenied):
        reg.authorize("L2", "read_via_semantic", "commerce", key_id="k-proj")


def test_p104_expired_key_denied():
    """有效期到期 → 拒绝（P1-04 ① 有效期/撤销状态）。"""
    from datetime import datetime, timedelta
    register_key("k-exp", {"read_via_semantic"}, tracks={"commerce"},
                 expires_at=datetime.now() - timedelta(hours=1))
    reg = ToolRegistry()
    with pytest.raises(PermissionDenied):
        reg.authorize("L2", "read_via_semantic", "commerce", key_id="k-exp")


def test_p104_revoked_key_denied():
    """吊销后任何动作被拒（P1-04 ① 撤销状态）。"""
    register_key("k-rev", {"read_via_semantic"}, tracks={"commerce"})
    reg = ToolRegistry()
    assert reg.authorize("L2", "read_via_semantic", "commerce",
                         key_id="k-rev").allowed
    revoke_key("k-rev")
    with pytest.raises(PermissionDenied):
        reg.authorize("L2", "read_via_semantic", "commerce", key_id="k-rev")


def test_p104_action_track_project_matrix():
    """动作 × key × track × project_id 全矩阵：全对才放行。"""
    register_key("k-matrix", {"read_via_semantic", "pipeline_rerun"},
                 tracks={"commerce"}, project_ids={"p1"}, agent_level="L2")
    reg = ToolRegistry()
    assert reg.authorize("L2", "read_via_semantic", "commerce",
                         key_id="k-matrix", project_id="p1").allowed
    for over in ({"action": "drop_schema"},
                 {"track": "research"},
                 {"project_id": "p2"},
                 {"agent_level": "L1", "action": "pipeline_rerun"}):  # L1 无此动作
        kw = {"agent_level": "L2", "action": "read_via_semantic", "track": "commerce",
              "key_id": "k-matrix", "project_id": "p1"}
        kw.update(over)
        with pytest.raises(PermissionDenied):
            reg.authorize(**kw)


# ---------------------------------------------------------------- P1-05

def test_p105_post_without_csrf_token_rejected(srv):
    """变更操作（POST）必须携带启动令牌/CSRF——没有就 403（P1-05 ①③）。"""
    body, ctype = _multipart(b"order_id,status\r\n1,paid\r\n")
    st, d = _post(srv.base, "/api/upload", None, data=body,
                  headers={"Content-Type": ctype})
    assert st == 403, f"无令牌的 POST 必须被拒，got {st}"
    assert "token" in d.get("error", "").lower() or "csrf" in d.get("error", "").lower()


def test_p105_session_endpoint_gives_token(srv):
    """本机会话认证：GET /api/session 返回随机启动令牌（P1-05 ①）。"""
    st, d = _get(srv.base, "/api/session")
    assert st == 200 and d.get("csrf_token") == srv.token


def test_p105_error_response_hides_absolute_paths():
    """错误响应不得把内部绝对路径返回前端（P1-05 ⑤）。"""
    msg = webapp._safe_error(ValueError(f"boom {webapp.ROOT}\\data\\x {webapp.ROOT}"))
    assert str(webapp.ROOT) not in msg
    assert "<repo>" in msg
    assert "boom" in msg


# ---------------------------------------------------------------- P1-06

def _conn_with(table_sql: str, rows_sql: str | None = None):
    import duckdb
    con = duckdb.connect()
    con.execute(table_sql)
    if rows_sql:
        con.execute(rows_sql)
    return con


def test_p106_string_filter_rejected():
    """filters 只收结构化 {field, op, value}，裸 SQL 字符串一律拒绝（P1-06 ②）。"""
    con = _conn_with(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR)",
        "INSERT INTO dwd_base VALUES (DATE '2026-08-26', 'paid')",
    )
    try:
        with pytest.raises(ContractError):
            compile_metric(con, "commerce", {
                "metric": "paid_orders", "type": "count", "grain": "order",
                "time_column": "created_at",
                "filters": ["status = 'paid'"],   # 旧格式
            })
    finally:
        con.close()


def test_p106_unknown_filter_field_rejected():
    """filter.field 必须来自已登记模型 schema，未知列编译期拒绝（P1-06 ③）。"""
    con = _conn_with(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR)",
        "INSERT INTO dwd_base VALUES (DATE '2026-08-26', 'paid')",
    )
    try:
        with pytest.raises(ContractError):
            compile_metric(con, "commerce", {
                "metric": "m1", "type": "count", "grain": "order",
                "time_column": "created_at",
                "filters": [{"field": "nonexistent_col", "op": "=", "value": 1}],
            })
    finally:
        con.close()


def test_p106_unknown_op_rejected():
    """操作符走 allowlist，LIKE 等未登记操作符编译期拒绝（P1-06 ③）。"""
    con = _conn_with(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR)",
        "INSERT INTO dwd_base VALUES (DATE '2026-08-26', 'paid')",
    )
    try:
        with pytest.raises(ContractError):
            compile_metric(con, "commerce", {
                "metric": "m2", "type": "count", "grain": "order",
                "time_column": "created_at",
                "filters": [{"field": "status", "op": "LIKE", "value": "pa%"}],
            })
    finally:
        con.close()


def test_p106_structured_filter_applied_and_parameterized():
    """结构化 filter 真正生效，且值参数化——注入值进不了 SQL 文本。"""
    con = _conn_with(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, amount DOUBLE, status VARCHAR)",
        "INSERT INTO dwd_base VALUES (DATE '2026-08-26', 10.0, 'paid'), "
        "(DATE '2026-08-26', 5.0, 'cancelled'), "
        "(DATE '2026-08-26', 7.0, 'paid'' OR ''1''=''1')",   # SQL 单引号加倍转义
    )
    try:
        compile_metric(con, "commerce", {
            "metric": "paid_amount", "type": "sum", "grain": "order",
            "time_column": "created_at", "numerator": {"metric": "amount"},
            "filters": [{"field": "status", "op": "=", "value": "paid"}],
        })
        rows = con.execute("SELECT value FROM paid_amount").fetchall()
        assert rows == [(10.0,)], f"过滤器未生效或被注入绕过：{rows}"
    finally:
        con.close()


def test_p106_unregistered_metric_rejected(tmp_path):
    """取数端只接受已登记 metric ID（P1-06 ⑤）——未编译登记即拒绝。"""
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR)")
    p = tmp_path / "never.yml"
    p.write_text(
        "metric: zz_never_compiled\ntype: count\ngrain: order\n"
        "time_column: created_at\nfilters: []\nversion: 1\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(SemanticQueryError):
            query_metric(con, tmp_path / "ledger.db", p, "commerce")
    finally:
        con.close()


def test_p106_cross_track_registration_enforced(tmp_path):
    """跨轨引用：research 编译的指标，commerce 取数必须拒绝（P1-06 负面⑥）。"""
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR)")
    con.execute("INSERT INTO dwd_base VALUES (DATE '2026-08-26', 'paid')")
    p = tmp_path / "ct.yml"
    p.write_text(
        "metric: ct_metric\ntype: count\ngrain: order\n"
        "time_column: created_at\nfilters: []\nversion: 1\n",
        encoding="utf-8",
    )
    try:
        compile_metric(con, "research", load_contract(p))   # 只在 research 登记
        with pytest.raises(SemanticQueryError):
            query_metric(con, tmp_path / "ledger.db", p, "commerce")
        q = query_metric(con, tmp_path / "ledger.db", p, "research")  # 本轨放行
        assert q["rows"] == [["2026-08-26", 1.0]]
    finally:
        con.close()


def test_p106_auto_test_order_filter_when_column_exists():
    """dwd 含 test_order 列时强制排除测试单（架构书清单 4-A / P2-04 场景 7）。"""
    con = _conn_with(
        "CREATE TABLE dwd_base (created_at TIMESTAMP, status VARCHAR, test_order BOOLEAN)",
        "INSERT INTO dwd_base VALUES "
        "(DATE '2026-08-26', 'paid', true), "
        "(DATE '2026-08-26', 'paid', false), "
        "(DATE '2026-08-26', 'cancelled', false)",
    )
    try:
        compile_metric(con, "commerce", {
            "metric": "real_orders", "type": "count", "grain": "order",
            "time_column": "created_at",
        })
        rows = con.execute("SELECT value FROM real_orders").fetchall()
        assert rows == [(2.0,)], f"测试单未被排除：{rows}"
    finally:
        con.close()


# ---------------------------------------------------------------- P1-07

def test_p107_concurrent_run_one_conflict(monkeypatch):
    """双线程并发 run：一条成功，另一条立即 RunConflict（409 语义）。"""
    def slow(upload_id):                     # 模拟慢管道，锁被占用
        time.sleep(0.4)
        return {"run_id": "r-x", "upload_id": upload_id}
    monkeypatch.setattr(webapp, "_run_pipeline", slow)
    out: dict = {}

    def worker():
        out["first"] = webapp.action_run("u1")
    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)                          # 确保第一条已拿锁
    with pytest.raises(webapp.RunConflict):
        webapp.action_run("u1")               # 第二条并发 → 409
    t.join(timeout=5)
    assert out["first"]["run_id"] == "r-x"


def test_p107_idempotency_returns_same_task(monkeypatch):
    """幂等键：重复请求返回同一任务，不重复建批次（P1-07 ⑤）。"""
    calls: list[str] = []
    monkeypatch.setattr(webapp, "_run_pipeline", lambda u: calls.append(u) or {"run_id": "r-1"})
    a = webapp.action_run("u1", idempotency_key="k-1")
    b = webapp.action_run("u1", idempotency_key="k-1")
    assert a is b and len(calls) == 1, f"幂等键未生效：calls={len(calls)}"


# ---------------------------------------------------------------- P1-08

def test_p108_multipart_preserves_original_bytes():
    """multipart 解析逐字节保真：尾随 CRLF/空行不能被 strip 吃掉（P1-08 ②）。"""
    file_bytes = b"order_id,status\r\n1,paid\r\n2,cancelled\r\n"   # 尾随 CRLF
    body, ctype = _multipart(file_bytes)
    name, content = webapp.parse_multipart_file(body, ctype)
    assert name == "t.csv"
    assert content == file_bytes, "multipart 解析改了原始字节"
    assert content.endswith(b"\r\n")


def test_p108_upload_sha_matches_original_bytes(srv):
    """上传时即时计算原始字节 SHA，落盘文件与浏览器上传字节逐位一致（P1-08 ③）。"""
    raw = b"a,b\r\n1,2\r\n" * 3               # 多个尾随/空行形态
    body, ctype = _multipart(raw)
    st, d = _post(srv.base, "/api/upload", srv.token, data=body,
                  headers={"Content-Type": ctype})
    assert st == 200, d
    assert d["sha256"] == hashlib.sha256(raw).hexdigest()
    idx = json.loads(webapp.UPLOAD_INDEX.read_text(encoding="utf-8"))
    disk = (webapp.UPLOAD_INDEX.parent / idx[d["upload_id"]]["file"]).read_bytes()
    assert disk == raw, "落盘字节与上传字节不一致"
