#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraceableMetrics V1.1 本地操作界面服务（零新增依赖 · 标准库实现）。

启动：仓库根目录下  python -c "import sys; sys.path.insert(0, r'<仓库根>'); import plugins.commerce.webapp as w; w.serve()"
浏览器：http://127.0.0.1:8765

页面 = 原型 V2 冻结的主旅程：投料 → 体检 → 修复 → 报告。
安全边界：只监听 127.0.0.1；只接受 CSV；文件大小 ≤ 20MB；写入仅落本轨卷。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys_path_injected = False

HOST, PORT = "127.0.0.1", 8765
MAX_UPLOAD = 20 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-一-龥]+")

_state: dict = {"runs": [], "uploads": {}}

# 审计 #11 / P1-07：共享 DuckDB/dbt 目标库，管道必须串行化——ThreadingHTTPServer
# 下两个并发 /api/run 同时写同一 db_path 会互相踩踏。
# V1 最小方案：已有任务在跑直接 409（不排队、不并行——排队会让用户以为卡死）。
_RUN_LOCK = threading.Lock()
# P1-07 第 5 条：/api/run 幂等键 → 重复请求返回同一任务，不重复建批次
_IDEMPOTENCY: dict[str, dict] = {}

# P1-05 ①：即使只监听回环，也使用随机启动令牌作本机会话认证——
# 页面 JS 与 /api/session 都拿到同一把 token，变更操作（POST）必须带上它。
_CSRF_TOKEN = secrets.token_hex(16)
# P1-05 ④：简单限速（每 IP 滑动窗口）+ 上传并发上限 + 请求体超时（见 Handler.setup）
_RATE: dict[str, list[float]] = {}
_RATE_WINDOW, _RATE_MAX = 60.0, 120
_UPLOAD_SEM = threading.BoundedSemaphore(2)
# P1-03：上传索引读写必须加锁（并发上传同时写 index.json 会互相覆盖）
_INDEX_LOCK = threading.Lock()


class RunConflict(RuntimeError):
    """P1-07：本轨已有运行在进行中——拒绝并发（HTTP 409）。"""


def _ensure_core() -> None:
    global sys_path_injected
    if not sys_path_injected:
        import sys
        sys.path.insert(0, str(ROOT))
        sys_path_injected = True


def _ctx():
    _ensure_core()
    from core.ingestion.context import TrackContext
    return TrackContext(track="commerce", volume_root=ROOT / "data" / "commerce")


# ---------- 业务动作 ----------

UPLOAD_INDEX = ROOT / "data" / "commerce" / "uploads" / "index.json"


def _safe_error(e: Exception) -> str:
    """P1-05 ⑤：错误响应不得把内部绝对路径和完整异常返回前端。

    只保留人类可读的一句，仓库绝对路径替换为 <repo>，其余盘符/UNC 路径抹掉。
    """
    msg = str(e) or type(e).__name__
    msg = msg.replace(str(ROOT), "<repo>")
    msg = re.sub(r"[A-Za-z]:[\\/][^\s'\"]{1,120}", "<path>", msg)
    msg = re.sub(r"\\\\[^\\\s'\"]{1,120}", "<unc-path>", msg)
    return (msg or "内部错误")[:400]


def _pipeline_error_payload(e: Exception) -> dict:
    """给界面返回可行动的管道失败信息，不暴露内部路径。"""
    return {
        "error": _safe_error(e),
        "code": "pipeline_failed",
        "stage": str(getattr(e, "pipeline_stage", "未知阶段")),
    }


def _persist_upload(uid: str, display: str, sha256: str, size: int) -> None:
    """P1-03：上传索引只保存相对文件名 + 元数据，绝不保存任意绝对路径。

    - 加锁 + 原子写（临时文件 → os.replace），并发上传不互相覆盖；
    - 索引损坏 → 显性抛错（P4），禁止静默重置为空（那等于无痕丢了审计事实）。
    """
    with _INDEX_LOCK:
        try:
            if UPLOAD_INDEX.exists():
                raw = UPLOAD_INDEX.read_text(encoding="utf-8").strip()
                idx = json.loads(raw) if raw else {}
                if not isinstance(idx, dict):
                    raise ValueError("upload index corrupted: not a mapping")
            else:
                idx = {}
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"上传索引损坏，拒绝覆盖：{e}（P4：不静默重置）") from e
        idx[uid] = {
            "file": uid,                       # 相对文件名（不可猜测对象 ID）
            "display": display,
            "sha256": sha256,
            "size": size,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "session": None,                   # V1 单用户本机；face=A 后填会话
        }
        tmp = UPLOAD_INDEX.with_name(UPLOAD_INDEX.name + ".tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, UPLOAD_INDEX)          # 原子替换：读方只会看到完整索引


# P1-08：上传时立即计算的原始字节指纹，贯穿 upload → raw manifest → run provenance。
# 落盘（而非只放内存），服务重启后仍可校验「落地批次 == 浏览器上传的原始文件」。
UPLOAD_SHA_INDEX = ROOT / "data" / "commerce" / "uploads" / "sha.json"

# P1-08：文件形态上限（超出即显性拒绝，不静默截断）
MAX_LINES = 200_000
MAX_COLUMNS = 1_000
MAX_FIELD_LEN = 10_000


def _persist_upload_sha(uid: str, sha: str) -> None:
    # 兼容旧版 sha.json（新索引已内嵌 sha256；保留写入以免降级路径失忆）
    try:
        d = json.loads(UPLOAD_SHA_INDEX.read_text(encoding="utf-8")) if UPLOAD_SHA_INDEX.exists() else {}
    except Exception:
        d = {}
    d[uid] = sha
    UPLOAD_SHA_INDEX.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _upload_sha(uid: str) -> str | None:
    """优先读新索引条目里的 sha256，旧库兼容回退 sha.json。"""
    try:
        if UPLOAD_INDEX.exists():
            d = json.loads(UPLOAD_INDEX.read_text(encoding="utf-8"))
            ent = d.get(uid)
            if isinstance(ent, dict) and ent.get("sha256"):
                return ent["sha256"]
        if UPLOAD_SHA_INDEX.exists():
            d = json.loads(UPLOAD_SHA_INDEX.read_text(encoding="utf-8"))
            return d.get(uid)
    except Exception:
        return None
    return None


def _verify_upload_sha(uid: str, path: Path) -> None:
    """P1-03 ⑤ / P1-08 ③：使用前再次核验文件 SHA-256，防 TOCTOU 替换。"""
    want = _upload_sha(uid)
    if not want:
        return                                    # 旧条目无指纹：不做无依据的拦截
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != want:
        raise ValueError(
            f"上传文件指纹不一致（{actual[:12]}… ≠ {want[:12]}…）——"
            f"磁盘文件已被改动，拒绝继续（P4/TOCTOU）"
        )


def _find_upload(uid: str) -> Path:
    """先查内存，再查磁盘索引——服务重启后旧 upload_id 依然可用。

    审计 #10 + P1-03 ②：索引只允许相对文件名，读取后 resolve 到本轨卷内——
    若 index.json 被篡改成卷外路径，assert_inside_volume 直接抛 PermissionError。
    """
    p = _state["uploads"].get(uid)
    if p and Path(p).exists():
        return _ctx().assert_inside_volume(Path(p))
    if UPLOAD_INDEX.exists():
        idx = json.loads(UPLOAD_INDEX.read_text(encoding="utf-8"))
        ent = idx.get(uid)
        if ent:
            rel = ent["file"] if isinstance(ent, dict) else ent
            # 只取 basename：索引里的路径成分一律视为文件名，杜绝目录穿越
            candidate = UPLOAD_INDEX.parent / Path(rel).name
            if candidate.exists():
                return _ctx().assert_inside_volume(candidate)
    raise KeyError(f"unknown upload_id: {uid}")


def _validate_csv_bytes(content: bytes) -> dict:
    """P1-08 第 4 条：形态校验分行/列/字段长度分别做，越界即显性拒绝。

    这里只做「能不能解析 + 是否超上限」，不改动任何一个字节。
    """
    if not content:
        raise ValueError("文件为空——没有内容可以分析")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"文件不是 UTF-8 编码（{e.reason}）——请先转成 UTF-8 再上传，"
            "不做编码猜测以免悄悄改数据"
        ) from e
    import csv as _csv

    reader = _csv.reader(io.StringIO(text, newline=""))
    rows = 0
    max_cols = 0
    try:
        for row in reader:
            rows += 1
            if rows > MAX_LINES:
                raise ValueError(f"文件超过 {MAX_LINES} 行上限（当前 V1 只支持小型数据集）")
            max_cols = max(max_cols, len(row))
            if max_cols > MAX_COLUMNS:
                raise ValueError(f"文件超过 {MAX_COLUMNS} 列上限")
            for cell in row:
                if len(cell) > MAX_FIELD_LEN:
                    raise ValueError(
                        f"存在超过 {MAX_FIELD_LEN} 字符的超长字段——疑似非表格数据"
                    )
    except _csv.Error as e:
        raise ValueError(f"CSV 解析失败：{e}") from e
    if rows < 2:
        raise ValueError("CSV 只有表头没有数据行——无法分析")
    return {"lines": rows, "columns": max_cols}


def action_upload(name: str, content: bytes) -> dict:
    if len(content) > MAX_UPLOAD:
        raise ValueError("文件超过 20MB 上限")
    if not name.lower().endswith((".csv", ".txt")):
        raise ValueError("V1.1 只支持 CSV（Excel 支持在 V1.2）")
    # P1-08 第 3 条：先算原始字节指纹（在任何写入/清洗之前），再贯穿到 provenance
    raw_sha = hashlib.sha256(content).hexdigest()
    shape = _validate_csv_bytes(content)
    safe = SAFE_NAME_RE.sub("_", name) or "upload.csv"
    tmp = ROOT / "data" / "commerce" / "uploads"
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / f"{uuid.uuid4().hex[:8]}__{safe}"
    p.write_bytes(content)                   # 原样落盘，绝不 strip/normalize
    _state["uploads"][p.name] = str(p)
    try:
        # P1-03：索引只存相对文件名 + 元数据（sha/大小/时间），加锁原子写
        _persist_upload(p.name, safe, raw_sha, len(content))
    except Exception:
        p.unlink(missing_ok=True)            # 索引失败不留孤儿文件
        _state["uploads"].pop(p.name, None)
        raise
    _persist_upload_sha(p.name, raw_sha)     # 兼容旧版 sha.json
    return {
        "upload_id": p.name,
        "name": safe,
        "size": len(content),
        "sha256": raw_sha,
        "lines": shape["lines"],
        "columns": shape["columns"],
    }


def action_checkup(upload_id: str) -> dict:
    _ensure_core()
    from core.quality.health import run_health_check
    p = _find_upload(upload_id)
    _verify_upload_sha(upload_id, p)         # P1-03 ⑤：用前核验指纹，防 TOCTOU 替换
    rep = run_health_check(p)
    return {
        "upload_id": upload_id,
        "rows": rep.rows,
        "columns": rep.columns,
        "issues": [
            {"code": i.code, "severity": i.severity, "human_text": i.human_text,
             "fix_suggestion": i.fix_suggestion, "impact": i.impact,
             "affected_rows": i.affected_rows}
            for i in rep.issues
        ],
    }


def action_run(upload_id: str, idempotency_key: str | None = None) -> dict:
    """管道入口：轨内串行（P1-07）。

    已有一个 run 在跑 → 抛 RunConflict（HTTP 409），不排队：排队会让用户以为
    服务卡死，而共享 DuckDB/dbt 目标并行跑实测会一条成功一条失败。
    带 Idempotency-Key 的重复请求直接返回首次结果，不重复建批次。
    """
    if idempotency_key:
        done = _IDEMPOTENCY.get(idempotency_key)
        if done is not None:
            return done
    if not _RUN_LOCK.acquire(blocking=False):
        raise RunConflict(
            "本轨已有一个管道正在运行，共享 DuckDB/dbt 目标不支持并行。"
            "请等当前任务结束后重试。"
        )
    try:
        result = _run_pipeline(upload_id)
    finally:
        _RUN_LOCK.release()
    if idempotency_key:
        _IDEMPOTENCY[idempotency_key] = result
    return result


def _query_ratio_components(
    conn, ledger_db: Path, contract_dir: Path, contract: dict,
    track: str, run_id: str,
) -> dict:
    """经 Semantic API 查询 ratio 的分子/分母，供业务解释与独立对账。"""
    if contract.get("type") != "ratio":
        return {}
    from core.semantic.api import query_metric
    from core.semantic.compiler import load_contract

    out: dict = {}
    for role in ("numerator", "denominator"):
        metric = (contract.get(role) or {}).get("metric")
        path = contract_dir / f"{metric}.yml"
        if not metric or not path.exists():
            continue
        sub = load_contract(path)
        q = query_metric(conn, ledger_db, path, track, run_id=run_id, limit=30)
        out[role] = {
            "metric": metric,
            "description": sub.get("description") or metric,
            "version": sub.get("version", 1),
            "query_id": q["query_id"],
            "series": q["rows"],
        }
    return out


def _run_pipeline(upload_id: str) -> dict:
    _ensure_core()
    ctx = _ctx()
    from core.ingestion.land import land_file
    from core.modeling.dbt import ModelingError, assert_dwd_built, run_dbt_model
    from core.quality.health import run_health_check
    from core.semantic.api import query_metric
    from core.semantic.compiler import compile_metric, load_contract
    from core.audit import ledger
    from core.audit.snapshot import publish_snapshot
    from core.quality.assertions import AssertionBlocked
    from plugins.commerce.assertions_hook import run_assertion_gate

    now = datetime.now()
    run_id = f"r-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    ledger_db = ROOT / "data" / "runs" / "ledger.db"
    # P0-03：任何阶段失败都要显性登记，stage 跟踪当前阶段
    stage: dict = {"name": "初始化", "batch_id": None}
    conn = None
    try:
        src = _find_upload(upload_id)

        # raw 落地（不可变批次）
        stage["name"] = "raw落地"
        manifest = land_file(ctx, "orders", src)
        stage["batch_id"] = manifest["batch_id"]
        # P1-08 第 3 条：上传时算的原始指纹必须等于落地批次指纹，
        # 任何一环改动过字节都在这里炸（不是等到报告出来才发现数字不对）
        want = _upload_sha(upload_id)
        if want and manifest.get("sha256") != want:
            raise ValueError(
                f"原始字节指纹不一致：浏览器上传 {want[:12]}… ≠ 落地批次 "
                f"{manifest['sha256'][:12]}…——拒绝继续（P4）"
            )

        # 体检（记录进台账）
        stage["name"] = "体检"
        health = run_health_check(src)

        # dbt 声明建模（从不可变批次读）+ 语义编译 + 指标（唯一取数端点：Semantic API）
        stage["name"] = "建模"
        run_dbt_model(ctx.db_path, Path(manifest["dest"]), select="dwd_base")
        conn = duckdb.connect(str(ctx.db_path))
        contract_path = ROOT / "schemas" / "metrics" / "commerce" / "pay_success_rate.yml"
        n = assert_dwd_built(conn)

        # 断言闸门（§08.1 三级断言引擎 · D2 断言同行 · S1 红灯封下游）：
        # 行数突变带 ±40% 熔断 + 关键列唯一/非空/枚举；blocking 失败 → 管道显性失败
        stage["name"] = "断言闸门"
        try:
            assertion_report = run_assertion_gate(conn, ROOT / "data" / "runs")
        except AssertionBlocked as ab:
            raise ModelingError(f"断言熔断（红灯封下游）：{ab}") from ab

        stage["name"] = "语义编译"
        contract = load_contract(contract_path)
        compile_metric(conn, ctx.track, contract, contracts_dir=contract_path.parent)
        stage["name"] = "指标查询"
        q = query_metric(conn, ledger_db, contract_path, ctx.track, run_id=run_id, limit=30)
        rows = q["rows"]
        metric_components = _query_ratio_components(
            conn, ledger_db, contract_path.parent, contract, ctx.track, run_id,
        )
        conn.execute("CHECKPOINT")
        conn.close()
        conn = None

        # 快照（P0-03：原子发布、同 id 拒绝覆盖）+ 台账（SQLite + JSON 双写）
        stage["name"] = "快照发布"
        snapshot_id = f"s-{run_id.removeprefix('r-')}"
        snap = publish_snapshot(ctx.db_path, ctx.snapshots_dir, snapshot_id, ctx.track)
        h = snap["sha256"]

        ledger_json = {
            "run_id": run_id,
            "track": ctx.track,
            "started_at": now.isoformat(timespec="seconds"),
            "batch_id": manifest["batch_id"],
            "snapshot_id": snapshot_id,
            "snapshot_sha256": h,
            "snapshot_size": snap["size"],
            "rows": health.rows,
            "metric": contract["metric"],
            "metric_version": contract.get("version", 1),
            "metric_series": rows,
            "metric_components": metric_components,
            "query_id": q["query_id"],
            "freshness": q["freshness"],
            "health_summary": health.summary,
            "assertion_report": assertion_report,
            "provenance": {
                "run_id": run_id,
                "snapshot_id": snapshot_id,
                "batch_id": manifest["batch_id"],
                "commit_sha": ledger.build_digest(),   # 审计 #5：代码版本不可缺
                # P0-06：代码版本之外，还要锁住依赖清单与指标契约口径
                "snapshot_sha256": h,
                "dependency_lock_digest": ledger.dependency_digest(),
                "metric_contract_digest": ledger.file_digest(contract_path),
                "metric_contract_version": contract.get("version", 1),
                "dirty": ledger.build_dirty(),
                "pipeline": "plugins/commerce/webapp.py",
                "raw_sha256": manifest["sha256"],
                "upload_sha256": want or manifest["sha256"],
            },
            "steps": [{"step": s, "ok": True} for s in
                      ("raw落地", "体检", "建模", "断言闸门", "语义编译", "指标查询", "快照+台账")],
        }
        stage["name"] = "台账落盘"
        ledger.write_run(ledger_db, ledger_json)
        runs_dir = ROOT / "data" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / f"{run_id}.json").write_text(
            json.dumps(ledger_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _state["runs"].insert(0, run_id)
        return ledger_json
    except Exception as e:  # noqa: BLE001 — P0-03：失败也必须显性登记
        ledger.write_run_failed(
            ledger_db, run_id, ctx.track, stage["name"],
            type(e).__name__, str(e),
            batch_id=stage.get("batch_id"),
            started_at=now.isoformat(timespec="seconds"),
        )
        # 保留原异常类型与 traceback，同时把当前阶段带到 HTTP 显性失败通道。
        # 测试/程序化调用仍可按原异常类型处理，不做静默包装或降级。
        try:
            e.pipeline_stage = stage["name"]
        except Exception:
            pass
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def action_agent_keys() -> dict:
    """API 看板（ADR-17 L2 白名单管理界面 · S2 判据的可视化）。

    逐 key 显示允许动作/轨道/级别/吊销状态；key 本身掩码显示（隐私）。
    写入白名单的唯一途径 = 环境变量 TRACEABLE_AGENT_KEY（用户自填，V1 单 key L1）。
    渲染前先走 _ensure_agent_registry()：环境变量里的 key 必须第一时间上板，
    否则「看板空 + 调用放行」的时序不一致会误导用户（首次访问就应如实显示）。
    """
    _ensure_agent_registry()
    _ensure_core()
    from core.auth.tool_registry import KEY_WHITELIST

    def _mask(k: str) -> str:
        return k[:4] + "…" + k[-4:] if len(k) > 8 else "****"

    rows = [{
        "key_id": _mask(kid),
        "actions": sorted(e["actions"]),
        "tracks": sorted(e["tracks"]),
        "agent_level": e.get("agent_level"),
        "revoked": bool(e.get("revoked", False)),
        "expires_at": str(e.get("expires_at") or ""),
    } for kid, e in KEY_WHITELIST.items()]
    return {"keys": rows}


_AGENT_KEYS_REGISTERED: set[str] = set()


def _ensure_agent_registry():
    """注册环境变量里的 Agent key（V1 单 key L1；key 由用户自行填写，不落盘）。

    未设置 TRACEABLE_AGENT_KEY → 白名单为空：任何 Agent 调用都被拒（默认拒绝）。
    """
    global _AGENT_KEYS_REGISTERED
    _ensure_core()
    from core.auth.tool_registry import ToolRegistry, register_key
    reg = ToolRegistry()
    key = os.environ.get("TRACEABLE_AGENT_KEY", "").strip()
    if key and key not in _AGENT_KEYS_REGISTERED:
        register_key(
            key, actions={"read_via_semantic"}, tracks={"commerce"}, agent_level="L1",
        )
        _AGENT_KEYS_REGISTERED.add(key)
    return reg


def action_agent(req: dict) -> dict:
    """ADR-17 Capability Matrix 运行时入口（S2 判据：per-key 白名单**生效**）。

    req = {key_id, action, track?, upload_id?}
    授权器（core.auth.tool_registry.authorize）按「能力矩阵 + per-key 白名单」裁决：
    - 越界动作 / 越界轨道 / 未注册 key / 已吊销 → PermissionDenied（403 + 审计）；
    - 放行动作（V1 L1 仅 read_via_semantic）：
        read_via_semantic —— 受限语义查询：最近一次成功 run 的指标序列 + claims 对账
                              （数字经快照重放验证，C-5：AI 只能读已对账的数字）；
        pipeline_rerun    —— L2 动作：L1 key 调用即被白名单拒绝（能力矩阵表语义）。
    - 每次调用都进 append-only 事件流（D8 审计全覆盖）。
    """
    _ensure_core()
    from core.auth.tool_registry import PermissionDenied
    from core.audit import ledger as _ledger

    key_id = str(req.get("key_id") or "")
    action = str(req.get("action") or "")
    track = str(req.get("track") or "commerce")
    if not key_id or not action:
        raise ValueError("agent 请求必须带 key_id 与 action")

    reg = _ensure_agent_registry()
    call = reg.authorize("L1", action, track, key_id=key_id)
    _ledger.log_event(
        ROOT / "data" / "runs" / "ledger.db",
        "agent_tool_call", subject_id=key_id[:8], track=track,
        payload={"action": action, "allowed": call.allowed, "note": call.audit_note},
    )
    if not call.allowed:                    # 防御：authorize 已抛，这里不应到达
        raise PermissionDenied(call.audit_note)

    if action == "read_via_semantic":
        runs = sorted((ROOT / "data" / "runs").glob("r-*.json"), reverse=True)
        if not runs:
            return {"ok": True, "action": action, "result": {"series": []}}
        rid = json.loads(runs[0].read_text(encoding="utf-8"))["run_id"]
        run = json.loads((ROOT / "data" / "runs" / f"{rid}.json").read_text(encoding="utf-8"))
        claims_result = action_claims(rid)  # 受限查询的数字同样过对账闸门（C-5）
        return {
            "ok": True, "action": action,
            "result": {
                "run_id": rid,
                "metric": run.get("metric"),
                "series": run.get("metric_series"),
                "reconcile_diff": claims_result.get("reconcile_diff"),
            },
        }
    if action == "pipeline_rerun":
        uid = str(req.get("upload_id") or "")
        if not uid:
            raise ValueError("pipeline_rerun 需要 upload_id")
        result = action_run(uid, idempotency_key=f"agent-{key_id[:8]}")
        return {"ok": True, "action": action, "result": {"run_id": result["run_id"]}}
    raise ValueError(f"未知动作: {action!r}")


def action_history() -> dict:
    runs_dir = ROOT / "data" / "runs"
    items = []
    for f in sorted(runs_dir.glob("r-*.json"), reverse=True)[:20]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            items.append({
                "run_id": d.get("run_id"), "started_at": d.get("started_at"),
                "rows": d.get("rows"), "metric": d.get("metric"),
                "issues": len((d.get("health_summary") or {}).get("issues", [])),
            })
        except Exception:
            continue
    return {"runs": items}


def action_explain(run_id: str) -> dict:
    """V1-DoD「LLM 解释(claims 过闸)」：结论对象 → Copilot 解释（六步管线）。

    - claims 先过对账闸门（复用 action_claims → build_conclusion），对账不通过 →
      build_conclusion 抛 ReportBlocked → 返回 gate_failed（显性，不渲染）；
    - Provider 未配置 → not_configured（显性）；解释未过 D6 数字对账闸门 →
      gate_failed——三种非 ok 状态都如实返回，绝不静默降级（P4）。
    """
    _ensure_core()
    from core.copilot.explain import ExplainGateError, explain
    from core.copilot.provider import OpenAICompatibleProvider, ProviderNotConfigured
    from core.report.kernel import ReportBlocked, build_conclusion

    claims_result = action_claims(run_id)
    lf = ROOT / "data" / "runs" / f"{run_id}.json"
    ledger_json = json.loads(lf.read_text(encoding="utf-8"))
    try:
        conclusion = build_conclusion(ledger_json, claims_result)
        # 智能说明是可选补充，不能用 Provider 的 90 秒默认超时阻塞报告交付。
        out = explain(conclusion, OpenAICompatibleProvider(timeout=10), face="C")
    except ProviderNotConfigured as e:
        return {"status": "not_configured", "reason": str(e)}
    except (ExplainGateError, ReportBlocked) as e:
        return {"status": "gate_failed", "reason": str(e)}
    except Exception as e:  # noqa: BLE001 — 端点不可达等运行期错误：显性标注，不 500 也不伪装
        return {"status": "unavailable", "reason": f"{type(e).__name__}: {e}"}
    return {
        "status": "ok",
        "narrative": out["narrative"],
        "claims": out["claims"],
        "provider": out["audit"]["provider"],
        "audit": out["audit"],
    }


def _contract_used_columns(contract: dict, contract_dir: Path, seen: set[str] | None = None) -> set[str]:
    """从指标契约递归提取事实列，用于判断体检问题是否影响当前指标。"""
    from core.semantic.compiler import load_contract

    seen = seen or set()
    metric = str(contract.get("metric") or "")
    if metric in seen:
        return set()
    seen = seen | {metric}
    used = {str(contract.get("time_column") or "")}
    used.update(
        str(f.get("field")) for f in (contract.get("filters") or [])
        if isinstance(f, dict) and f.get("field")
    )
    if contract.get("order_key"):
        used.add(str(contract["order_key"]))
    roles = ("numerator", "denominator") if contract.get("type") == "ratio" else ("numerator",)
    for role in roles:
        name = (contract.get(role) or {}).get("metric")
        if not name:
            continue
        sub_path = contract_dir / f"{name}.yml"
        if sub_path.exists():
            used.update(_contract_used_columns(load_contract(sub_path), contract_dir, seen))
        elif contract.get("type") in ("sum", "avg", "ratio"):
            used.add(str(name))
    used.discard("")
    return used


_COMMERCE_COLUMN_LABELS = {
    "amount_total": "订单应付金额",
    "amount_paid": "订单实付金额",
    "created_at": "下单时间",
    "paid_at": "支付时间",
    "order_id": "订单编号",
    "pay_status": "支付状态",
}


def _humanize_health_issue(issue: dict) -> dict:
    """把商用数据字段翻译成报告读者能理解的业务名称。"""
    human = dict(issue)
    column = str(issue.get("column") or "")
    label = _COMMERCE_COLUMN_LABELS.get(column)
    if not label:
        return human
    affected = issue.get("affected_rows", "—")
    if issue.get("code") == "blank_value":
        human["human_text"] = f"{label}有 {affected} 行空白"
    elif issue.get("code") == "date_format_mixed":
        human["human_text"] = f"{label}混用了不同的日期写法"
    else:
        human["human_text"] = str(issue.get("human_text") or "").replace(column, label)
    return human


def _build_business_context(
    run: dict, claims_result: dict, contract: dict, contract_dir: Path,
) -> dict:
    """只用契约和已对账 claims 生成业务文案所需的结构化事实。"""
    description = str(contract.get("description") or contract.get("metric") or "指标")
    display_name = description.split("=", 1)[0].strip()
    components: dict = {}
    for role, rec in (claims_result.get("component_claims") or {}).items():
        clean = rec.get("reconcile_diff") == "clean"
        verified = rec.get("claims") or []
        if not clean or not verified:
            continue
        latest = verified[-1]
        label = str(rec.get("description") or rec.get("metric") or role)
        label = label.split("（", 1)[0].strip()
        components[role] = {
            "metric": rec.get("metric"),
            "label": label,
            "sentence_label": (
                "支付成功" if rec.get("metric") == "pay_success_orders"
                else "发起支付订单" if rec.get("metric") == "pay_attempted_orders"
                else label.removesuffix("数")
            ),
            "value": latest.get("value"),
            "period": latest.get("period"),
            "reconcile": latest.get("reconcile"),
            "source": latest.get("source"),
        }

    health = run.get("health_summary") or {}
    details = health.get("issue_details") or []
    human_details = [_humanize_health_issue(i) for i in details]
    if not details:
        quality_impact = (
            "数据体检未发现问题。" if not health.get("issue_count") else
            f"数据体检发现 {health.get('issue_count')} 个问题；该旧运行未保存详细列信息。"
        )
    else:
        used = _contract_used_columns(contract, contract_dir)
        affected = {str(i.get("column")) for i in details if i.get("column")}
        issue_text = "；".join(str(i.get("human_text")) for i in human_details)
        if affected and affected.isdisjoint(used):
            quality_impact = (
                f"体检发现：{issue_text}。这些问题所在字段不参与本指标计算，"
                "因此不改变本次结果；分析这些字段时仍需先处理。"
            )
        else:
            quality_impact = f"体检发现：{issue_text}。问题涉及本指标使用字段，解读时需谨慎。"

    return {
        "display_name": display_name,
        "definition": description,
        "unit": "笔",
        "components": components,
        "quality_impact": quality_impact,
        "health_issues": human_details,
    }


def action_export(run_id: str) -> dict:
    """导出 HTML 报告（D12 主旅程第四段：分享）。

    对账不通过时 core.report.export 会抛 ReportBlocked——不落任何文件（ADR-18）。
    LLM 解释（V1-DoD）在导出前生成：未配置/未过闸时照样导出，但报告内显性标注，
    不伪装成品（P4）。
    """
    _ensure_core()
    from core.report.export import export_report

    claims = action_claims(run_id)          # 先过对账闸门
    lf = ROOT / "data" / "runs" / f"{run_id}.json"
    ledger_json = json.loads(lf.read_text(encoding="utf-8"))
    ctx = _ctx()
    llm = action_explain(run_id)            # V1-DoD：LLM 解释（未配置→显性标注）
    from core.semantic.compiler import load_contract
    contract_path = ROOT / "schemas" / "metrics" / "commerce" / f"{ledger_json['metric']}.yml"
    contract = load_contract(contract_path)
    business_context = _build_business_context(
        ledger_json, claims, contract, contract_path.parent,
    )
    meta = export_report(
        ledger_json, claims,
        out_dir=ctx.volume_root / "exports",
        ledger_db=ROOT / "data" / "runs" / "ledger.db",
        llm_explanation=llm,
        business_context=business_context,
    )
    return {**meta, "content": Path(meta["path"]).read_bytes()}


def action_claims(run_id: str) -> dict:
    """ADR-18 V1.1 最小版：报告数字自动生成 claims 并与快照库重放对账。

    对账逻辑在共享核心 core.claims.reconcile（管道全局唯一），本函数只做编排。
    """
    _ensure_core()
    from core.claims.source import SourceRequest, reconcile_verified
    from core.report.schema import TRACKS
    from core.semantic.compiler import load_contract

    lf = ROOT / "data" / "runs" / f"{run_id}.json"
    if not lf.exists():
        raise ValueError(f"run not found: {run_id}")
    run = json.loads(lf.read_text(encoding="utf-8"))
    # P0-07：track 缺失/非法一律拒绝，不默认 commerce（默认会静默串轨）
    track = run.get("track")
    if track not in TRACKS:
        raise ValueError(f"run {run_id} 的 track 缺失或非法（{track!r}）——拒绝继续")
    series = run.get("metric_series") or []
    if not series:
        raise ValueError("run has no metric series")

    # 审计 #2：claims 必须绑定一次真实注册过的查询——query_id 缺失时
    # 不得用 "replay:xxx" 伪 id 顶替（那不是一次真实查询，source 断链）
    query_id = run.get("query_id")
    if not query_id:
        raise ValueError(f"run {run_id} 缺 query_id——claims 无法绑定真实查询（ADR-18）")
    sid = run.get("snapshot_id")
    if not sid:
        raise ValueError(f"run {run_id} 缺 snapshot_id——无法对账")

    # P0-04：只允许按 run 台账里的精确快照路径加载，没有任何 fallback
    # （旧实现里 glob 找别的快照顶替，会产生「碰巧一样」的假 PASS）
    snap = _ctx().snapshots_dir / sid / f"{track}.db"

    contract_path = ROOT / "schemas" / "metrics" / track / f"{run['metric']}.yml"
    contract = load_contract(contract_path) if contract_path.exists() else None

    req = SourceRequest(
        ledger_db=ROOT / "data" / "runs" / "ledger.db",
        snapshot_file=snap,
        query_id=query_id,
        run_id=run["run_id"],
        snapshot_id=sid,
        track=track,
        metric=run["metric"],
    )
    claims = [{
        "metric": run["metric"],
        "period": dt,
        "operation": "point",
        "value": value,
        "unit": "ratio",
        "source": {"run_id": run["run_id"], "snapshot_id": sid, "query_id": query_id},
    } for dt, value in series[:5]]
    # P0-02：先证明 source 真实闭合，再对账——事实源只来自该精确快照重放
    result = reconcile_verified(claims, req, contract=contract)
    component_results: dict = {}
    for role, component in (run.get("metric_components") or {}).items():
        metric_name = component.get("metric")
        query = component.get("query_id")
        series = component.get("series") or []
        sub_path = contract_path.parent / f"{metric_name}.yml"
        if not metric_name or not query or not series or not sub_path.exists():
            continue
        sub_contract = load_contract(sub_path)
        sub_req = SourceRequest(
            ledger_db=ROOT / "data" / "runs" / "ledger.db",
            snapshot_file=snap,
            query_id=query,
            run_id=run["run_id"],
            snapshot_id=sid,
            track=track,
            metric=metric_name,
        )
        sub_claims = [{
            "metric": metric_name,
            "period": dt,
            "operation": "point",
            "value": value,
            "unit": "count",
            "source": {"run_id": run["run_id"], "snapshot_id": sid, "query_id": query},
        } for dt, value in series[:5]]
        rec = reconcile_verified(sub_claims, sub_req, contract=sub_contract)
        component_results[role] = {
            "metric": metric_name,
            "description": component.get("description") or metric_name,
            **rec,
        }

    result["component_claims"] = component_results
    blocked_components = [
        rec for rec in component_results.values()
        if rec.get("reconcile_diff") != "clean"
    ]
    if blocked_components:
        result["reconcile_diff"] = "MISMATCH"
        result["quality_gate"] = "blocked"
        result["failed"] = (result.get("failed") or []) + [
            claim
            for rec in blocked_components
            for claim in (rec.get("failed") or [])
        ]
    return result


# ---------- HTTP 层 ----------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TraceableMetrics · 数据分析自动化</title>
<style>
:root{--bg:#f4f6f6;--paper:#fcfcfc;--ink:#16191c;--ink2:#3e464d;--muted:#5b656d;--faint:#8a949b;
--line:#dde1e3;--soft:#e8ebec;--panel:#f1f3f3;--accent:#0f6f68;--accent-soft:#e4efee;
--ok:#1c7c46;--warn:#9a6a12;--bad:#b53a2a;
--font:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;--mono:"Cascadia Code",Consolas,monospace;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--ink);font-family:var(--font);font-size:13.5px;line-height:1.65;}
.band{height:4px;background:var(--accent);}
.topbar{background:#12181a;color:#e8ecec;display:flex;align-items:center;gap:18px;padding:10px 18px;}
.logo{font-family:var(--mono);font-size:12px;letter-spacing:.12em;}
.logo b{color:#fff;}
.chip{margin-left:auto;font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:#7fbfb8;border:1px solid rgba(255,255,255,.14);padding:3px 8px;}
.wrap{max-width:860px;margin:0 auto;padding:28px 22px 80px;}
h1{font-size:22px;font-weight:600;letter-spacing:-.01em;}
.sub{color:var(--muted);font-size:12.5px;margin:4px 0 18px;}
.card{background:var(--paper);border:1px solid var(--line);padding:16px 18px;margin-bottom:14px;}
.card h3{font-size:13.5px;font-weight:600;margin-bottom:8px;}
.drop{border:1.5px dashed var(--faint);padding:34px;text-align:center;cursor:pointer;}
.drop.over{border-color:var(--accent);background:var(--accent-soft);}
.drop .big{font-size:14px;font-weight:600;margin-top:8px;}
.drop .hint{font-size:11.5px;color:var(--muted);margin-top:4px;}
.btn{border:1px solid var(--line);background:var(--paper);color:var(--ink);padding:8px 16px;font:inherit;font-size:12.5px;cursor:pointer;border-radius:2px;}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
.btn:disabled{opacity:.45;cursor:not-allowed;}
.kv{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--soft);font-size:12.5px;color:var(--ink2);}
.kv b{font-family:var(--mono);font-weight:600;color:var(--ink);}
.issue{border:1px solid var(--soft);padding:10px 12px;margin-top:8px;}
.issue .h{font-weight:600;font-size:12.5px;}
.issue .s{font-size:11.5px;color:var(--muted);margin:3px 0;}
.issue .fix{font-size:11.5px;color:var(--ok);}
.hidden{display:none;}
.metric-big{font-family:var(--mono);font-size:30px;font-weight:600;color:var(--accent);}
.tag-ok{color:var(--ok);font-weight:600;}
.tag-warn{color:var(--warn);font-weight:600;}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px;}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--soft);}
th{font-size:10.5px;letter-spacing:.05em;color:var(--muted);}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#12181a;color:#fff;padding:10px 18px;font-size:12.5px;border-radius:2px;opacity:0;transition:opacity .3s;z-index:50;}
.toast.show{opacity:1;}
.run-status{border-left:3px solid var(--faint);background:var(--panel);padding:9px 12px;margin-top:12px;font-size:12px;color:var(--ink2);}
.run-status.running{border-left-color:var(--accent);}
.run-status.ok{border-left-color:var(--ok);}
.run-status.error{border-left-color:var(--bad);color:var(--bad);}
.report-tabs{display:flex;border-bottom:1px solid var(--line);margin:18px 0 14px;gap:2px;}
.report-tab{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);padding:9px 14px;font:inherit;font-size:12.5px;cursor:pointer;}
.report-tab.active{border-bottom-color:var(--accent);color:var(--ink);font-weight:600;}
.report-tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.preview-shell{border:1px solid var(--line);background:var(--panel);padding:10px;margin-bottom:14px;}
.preview-frame{display:block;width:100%;height:min(720px,72vh);min-height:480px;border:1px solid var(--line);background:#fff;}
.report-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;}
#rp-share-body{min-width:0;overflow-wrap:anywhere;}
#rp-share-name,#rp-share-path,#rp-share-sha{word-break:break-all;overflow-wrap:anywhere;}
details{border:1px solid var(--soft);background:var(--panel);padding:9px 13px;margin-top:10px;}
summary{cursor:pointer;font-size:12px;color:var(--muted);}
.diag{font-size:12px;color:var(--ink2);margin-top:7px;line-height:1.8;}
@media (max-width:600px){
  .topbar{gap:8px;padding:8px 12px;}
  .logo{font-size:10.5px;letter-spacing:.06em;}
  .chip{font-size:9px;letter-spacing:.04em;padding:2px 5px;}
  .wrap{padding:20px 14px 56px;}
  .card{padding:14px;min-width:0;}
  .preview-shell{padding:6px;}
  .report-actions{gap:7px;}
  .report-actions .btn{padding:8px 11px;}
}
</style>
</head>
<body>
<div class="topbar"><span class="logo"><b>TRACEABLE</b> 本地工作台</span><span class="chip">商用分析 · 本机运行</span></div>
<div class="band"></div>
<div class="wrap">

<section id="pg-intake">
  <h1>数据投料</h1>
  <div class="sub">把 CSV 拖进来 → 自动体检 → 生成指标报告 · 原始文件永不改动</div>
  <div class="card">
    <div class="drop" id="drop">
      <div class="big">把 CSV 拖到这里，或点击选择文件</div>
      <div class="hint">≤ 20MB · 自动体检 · 生成带快照与对账的报告</div>
    </div>
    <input type="file" id="file" accept=".csv,.txt" class="hidden">
  </div>
  <div class="card hidden" id="checkup-card">
    <h3>体检报告 <span id="ck-file" style="color:var(--muted);font-weight:400;"></span></h3>
    <div class="kv"><span>总行数</span><b id="ck-rows">—</b></div>
    <div class="kv"><span>识别列数</span><b id="ck-cols">—</b></div>
    <div class="kv"><span>发现的问题</span><b id="ck-issues">—</b></div>
    <div id="ck-issue-list"></div>
    <details><summary>为什么这些问题重要？</summary><div class="diag">空白金额会让汇总偏低；混用的日期格式会让按日对比失效。采纳修复建议不会改动你的原始文件——清洗是新建批次，随时可撤销。</div></details>
    <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;">
      <button class="btn primary" id="btn-run">生成指标报告 →</button>
      <button class="btn" id="btn-reset">重新选文件</button>
    </div>
    <div id="run-status" class="run-status hidden" role="status" aria-live="polite"></div>
  </div>
</section>

<section id="pg-report" class="hidden">
  <h1>支付成功率报告</h1>
  <div class="sub" id="rp-meta">—</div>
  <div id="rp-output-status" class="run-status ok" role="status" aria-live="polite">指标报告已生成（当前页面），尚未下载报告文件。需要保存或发送时，请点击下方“下载报告文件”。</div>
  <div class="report-tabs" role="tablist" aria-label="报告视图">
    <button class="report-tab active" id="tab-summary" role="tab" aria-selected="true" aria-controls="report-summary">结果概览</button>
    <button class="report-tab" id="tab-preview" role="tab" aria-selected="false" aria-controls="report-preview">报告预览</button>
  </div>
  <div id="report-summary" role="tabpanel" aria-labelledby="tab-summary">
    <div class="card">
      <h3>支付成功率</h3>
      <div class="metric-big" id="rp-value">—</div>
      <div style="font-size:11.5px;color:var(--muted);" id="rp-latest-dt"></div>
      <div class="diag" id="rp-business-summary">正在核对成功订单数与发起支付订单数…</div>
      <div class="run-status" id="rp-business-definition">支付成功率 = 成功支付订单数 ÷ 发起支付订单数（按订单去重）</div>
    </div>
    <div class="card">
      <h3>日粒度序列</h3>
      <table id="rp-table"><thead><tr><th>日期</th><th>值</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card" id="rp-claims">
      <h3>数据可信度 <span id="rp-gate" class="tag-ok"></span></h3>
      <div id="rp-claims-body" style="font-size:12px;color:var(--ink2);"></div>
      <details><summary>技术详情</summary><div class="diag" id="rp-technical-details" style="white-space:pre-wrap;"></div><div class="diag" id="rp-explain-technical"></div></details>
    </div>
    <div class="card hidden" id="rp-explain">
      <h3>智能补充说明 <span id="rp-explain-status" class="tag-ok"></span></h3>
      <div id="rp-explain-body" style="font-size:12px;color:var(--ink2);line-height:1.9;"></div>
    </div>
  </div>
  <div id="report-preview" class="hidden" role="tabpanel" aria-labelledby="tab-preview">
    <div class="preview-shell">
      <div id="rp-preview-status" class="run-status" role="status" aria-live="polite">打开预览时会生成最终 HTML 文件；也可以不预览，直接下载。</div>
      <iframe id="rp-preview-frame" class="preview-frame" title="最终报告文件预览"></iframe>
    </div>
    <div class="card hidden" id="rp-share">
      <h3>分享报告</h3>
      <div id="rp-share-body" style="font-size:12px;color:var(--ink2);line-height:1.9;">
        <div>这份 HTML 文件可直接发送给他人。</div>
        <div><b id="rp-share-name"></b></div>
        <div>服务器副本：<span id="rp-share-path" style="font-family:var(--mono);font-size:11px"></span></div>
        <div>文件指纹：<span id="rp-share-sha" style="font-family:var(--mono);font-size:11px"></span></div>
      </div>
      <details><summary>这个报告凭什么可信？</summary><div class="diag">报告技术附录带有运行编号、数据快照号和原始文件指纹。拿着这些标识可以在系统里复算出完全一样的数字。</div></details>
    </div>
  </div>
  <div class="report-actions">
    <button class="btn primary" id="btn-export">下载报告文件</button>
    <button class="btn" id="btn-again">再投一份</button>
    <button class="btn" id="btn-history">查看历史运行</button>
  </div>
</section>

<section id="pg-history" class="hidden">
  <h1>历史运行</h1>
  <div class="sub">每次运行的台账都在 data/runs/ 落盘，数字可追溯到 raw 文件</div>
  <div class="card"><table id="hs-table"><thead><tr><th>运行</th><th>时间</th><th>行数</th><th>体检问题</th></tr></thead><tbody></tbody></table></div>
  <button class="btn" id="btn-back">返回投料</button>
</section>

</div>
<div class="toast" id="toast"></div>

<script>
var $=function(s){return document.querySelector(s);};
function toast(m){var t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(function(){t.classList.remove('show');},3200);}
function show(id){['pg-intake','pg-report','pg-history'].forEach(function(p){$('#'+p).classList.toggle('hidden',p!==id);});}
function setRunStatus(kind,message){var s=$('#run-status');s.className='run-status '+kind;s.textContent=message;}
var uploadId=null, uploadName='', currentRunId=null;
var reportArtifact=null, reportArtifactPromise=null, reportArtifactUrl=null;
// P1-05 ①：服务端启动时生成的随机令牌（本机会话凭据），POST 必须携带
var CSRF_TOKEN="__CSRF_TOKEN__";

var dz=$('#drop'),fi=$('#file');
dz.addEventListener('click',function(){fi.click();});
dz.addEventListener('dragover',function(e){e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',function(){dz.classList.remove('over');});
dz.addEventListener('drop',function(e){e.preventDefault();dz.classList.remove('over');if(e.dataTransfer.files.length)upload(e.dataTransfer.files[0]);});
fi.addEventListener('change',function(){if(fi.files.length)upload(fi.files[0]);});

function upload(f){
  if(f.size>20*1024*1024){toast('文件超过 20MB 上限');return;}
  uploadName=f.name;
  // 注意：拖拽路径不会写入 file input，后续不能读 files[0]，文件名在此缓存
  var fd=new FormData();fd.append('file',f,f.name);
  fetch('/api/upload',{method:'POST',body:fd,headers:{'X-CSRF-Token':CSRF_TOKEN}}).then(function(r){return r.json();}).then(function(d){
    if(d.error){toast(d.error);return;}
    uploadId=d.upload_id;return checkup();
  }).catch(function(){toast('上传失败——服务是否在运行？');});
}

function checkup(){
  // P2-02：拖拽上传不会写入 file input，files[0] 为 undefined——
  // 旧实现在这里静默抛 TypeError（拖拽分支直接崩）。文件名统一取 uploadName 缓存。
  $('#ck-file').textContent='· '+(uploadName||'未命名文件');
  fetch('/api/checkup?id='+encodeURIComponent(uploadId)).then(function(r){return r.json();}).then(function(d){
    if(d.error){toast(d.error);return;}
    $('#ck-rows').textContent=d.rows;
    $('#ck-cols').textContent=d.columns.length+'（'+d.columns.join('、')+'）';
    $('#ck-issues').textContent=d.issues.length?d.issues.length+'（见下）':'0 · 数据干净';
    var box=$('#ck-issue-list');box.innerHTML='';
    d.issues.forEach(function(i){
      var el=document.createElement('div');el.className='issue';
      el.innerHTML='<div class="h">「'+i.human_text+'」</div><div class="s">影响：'+i.impact+'（涉及 '+i.affected_rows+' 行）</div><div class="fix">修复建议：'+i.fix_suggestion+'</div>';
      box.appendChild(el);
    });
    $('#pg-intake').classList.remove('hidden');$('#checkup-card').classList.remove('hidden');
    $('#checkup-card').scrollIntoView({behavior:'smooth'});
  });
}

$('#btn-run').addEventListener('click',function(){
  var b=this;if(b.disabled)return;
  b.disabled=true;b.textContent='正在生成…';
  setRunStatus('running','正在生成指标报告，请保持本页打开。完成后会自动进入报告页。');
  fetch('/api/run?id='+encodeURIComponent(uploadId),{method:'POST',headers:{'X-CSRF-Token':CSRF_TOKEN}}).then(function(r){return r.json();}).then(function(d){
    b.disabled=false;b.textContent='生成指标报告 →';
    if(d.error){
      var stage=d.stage||'未知阶段';
      setRunStatus('error','报告未生成。失败阶段：'+stage+'。详情：'+d.error+'。请修正后再重试。');
      toast('报告未生成，请查看页面上的失败说明');return;
    }
    renderReport(d);
  }).catch(function(e){
    b.disabled=false;b.textContent='生成指标报告 →';
    setRunStatus('error','报告未生成。无法连接本地服务：'+e.message+'。请确认工作台仍在运行后重试。');
    toast('报告未生成，请查看页面上的失败说明');
  });
});

function resetReportArtifact(){
  reportArtifact=null;reportArtifactPromise=null;
  if(reportArtifactUrl){URL.revokeObjectURL(reportArtifactUrl);reportArtifactUrl=null;}
  $('#rp-preview-frame').removeAttribute('src');
  $('#rp-share').classList.add('hidden');
  $('#rp-preview-status').className='run-status';
  $('#rp-preview-status').textContent='打开预览时会生成最终 HTML 文件；也可以不预览，直接下载。';
}

function fillShareInfo(o){
  $('#rp-share-name').textContent=o.name;
  $('#rp-share-path').textContent=o.path;
  $('#rp-share-sha').textContent=o.sha;
  $('#rp-share').classList.remove('hidden');
}

function ensureReportArtifact(){
  if(reportArtifact){return Promise.resolve(reportArtifact);}
  if(reportArtifactPromise){return reportArtifactPromise;}
  reportArtifactPromise=fetch('/api/export?run='+encodeURIComponent(currentRunId)).then(function(r){
    if(!r.ok){throw new Error('服务端拒绝：报告未过对账闸门');}
    var name=decodeURIComponent(r.headers.get('X-Export-Name')||'report.html');
    var path=decodeURIComponent(r.headers.get('X-Export-Path')||'');
    var sha=r.headers.get('X-Export-Sha256')||'';
    return r.blob().then(function(blob){return {name:name,path:path,sha:sha,blob:blob};});
  }).then(function(o){
    reportArtifact=o;
    reportArtifactUrl=URL.createObjectURL(o.blob);
    fillShareInfo(o);
    reportArtifactPromise=null;
    return o;
  },function(e){reportArtifactPromise=null;throw e;});
  return reportArtifactPromise;
}

function showReportPreview(){
  if(!currentRunId){return;}
  var status=$('#rp-preview-status');
  status.className='run-status running';
  status.textContent='正在生成最终报告预览…';
  ensureReportArtifact().then(function(){
    $('#rp-preview-frame').src=reportArtifactUrl;
    status.className='run-status ok';
    status.textContent='下方是最终 HTML 文件预览。预览不是下载前置步骤，可随时直接下载。';
  }).catch(function(e){
    status.className='run-status error';
    status.textContent='预览生成失败：'+e.message;
  });
}

function switchReportTab(name){
  var preview=name==='preview';
  $('#report-summary').classList.toggle('hidden',preview);
  $('#report-preview').classList.toggle('hidden',!preview);
  $('#tab-summary').classList.toggle('active',!preview);
  $('#tab-preview').classList.toggle('active',preview);
  $('#tab-summary').setAttribute('aria-selected',preview?'false':'true');
  $('#tab-preview').setAttribute('aria-selected',preview?'true':'false');
  if(preview){showReportPreview();}
}

$('#tab-summary').addEventListener('click',function(){switchReportTab('summary');});
$('#tab-preview').addEventListener('click',function(){switchReportTab('preview');});

function renderReport(d){
  currentRunId=d.run_id;
  resetReportArtifact();
  switchReportTab('summary');
  var series=d.metric_series||[];
  if(series.length){
    var latest=series[0];
    $('#rp-value').textContent=(latest[1]*100).toFixed(2)+'%';
    $('#rp-latest-dt').textContent='数据日期 '+latest[0]+' · 基于 '+d.rows+' 行';
  }
  $('#rp-meta').textContent='数据日期 '+(series.length?series[0][0]:'—')+' · '+d.rows+' 行明细 · 体检问题 '+(d.health_summary?d.health_summary.issue_count:0)+' 个';
  var tb=$('#rp-table tbody');tb.innerHTML='';
  (d.metric_series||[]).forEach(function(r){
    var tr=document.createElement('tr');
    tr.innerHTML='<td>'+r[0]+'</td><td class="mono">'+(r[1]*100).toFixed(2)+'%</td>';
    tb.appendChild(tr);
  });
  $('#rp-output-status').className='run-status ok';
  $('#rp-output-status').textContent='指标报告已生成（当前页面），尚未下载报告文件。需要保存或发送时，请点击下方“下载报告文件”。';
  show('pg-report');
  fetch('/api/claims?run='+encodeURIComponent(d.run_id)).then(function(r){return r.json();}).then(function(c){
    if(c.error){$('#rp-claims-body').textContent='数据核对未完成：'+c.error;return;}
    $('#rp-gate').textContent=c.reconcile_diff==='clean'?'✓ 已核对':'✗ 存在不一致';
    $('#rp-gate').className=c.reconcile_diff==='clean'?'tag-ok':'tag-warn';
    $('#rp-claims-body').textContent=c.reconcile_diff==='clean'
      ?'报告数字已使用同一份数据快照重新计算，结果一致。'
      :'数字核对不一致，本报告不可使用。';
    var cc=c.component_claims||{},num=cc.numerator,den=cc.denominator;
    if(num&&den&&num.reconcile_diff==='clean'&&den.reconcile_diff==='clean'){
      var nc=num.claims[num.claims.length-1],dc=den.claims[den.claims.length-1];
      $('#rp-business-summary').textContent=dc.value.toFixed(0)+' 笔发起支付订单中，'
        +nc.value.toFixed(0)+' 笔支付成功。当前只有一个日期的数据，无法判断比平时更好还是更差。';
    }else{
      $('#rp-business-summary').textContent='本页显示当天支付成功的订单占全部发起支付订单的比例。';
    }
    var lines=(c.claims||[]).map(function(cl){return cl.metric+' · '+cl.period+' · '
      +cl.reconcile+' · '+cl.source.snapshot_id+'/'+cl.source.query_id;});
    Object.keys(cc).forEach(function(role){(cc[role].claims||[]).forEach(function(cl){
      lines.push(role+' '+cl.metric+' · '+cl.value+' · '+cl.reconcile+' · '
        +cl.source.snapshot_id+'/'+cl.source.query_id);
    });});
    $('#rp-technical-details').textContent=lines.join('\\n');
  });
  fetch('/api/explain?run='+encodeURIComponent(d.run_id)).then(function(r){return r.json();}).then(function(x){
    var box=$('#rp-explain');
    if(x.status==='ok'){
      $('#rp-explain-status').textContent='✓ 数字已核对';
      $('#rp-explain-body').textContent=x.narrative;
      box.classList.remove('hidden');
    }else{
      $('#rp-explain-technical').textContent='智能说明暂不可用，不影响指标计算和数据核对。技术状态：'
        +x.status+'；'+(x.reason||'未配置');
    }
  });
  window.scrollTo(0,0);
}

$('#btn-export').addEventListener('click',function(){
  var b=this;if(!currentRunId){toast('请先生成报告');return;}
  b.disabled=true;b.textContent='正在生成…';
  ensureReportArtifact().then(function(o){
    var a=document.createElement('a');
    a.href=reportArtifactUrl;a.download=o.name;document.body.appendChild(a);a.click();
    document.body.removeChild(a);
    $('#rp-output-status').className='run-status ok';
    $('#rp-output-status').textContent='报告文件已生成并开始下载。服务器副本保存位置：'+o.path;
    toast('报告已下载');
    b.disabled=false;b.textContent='再下载一次';
  }).catch(function(e){
    toast('导出失败：'+e.message);
    $('#rp-output-status').className='run-status error';
    $('#rp-output-status').textContent='报告页面已生成，但报告文件下载失败：'+e.message;
    b.disabled=false;b.textContent='下载报告文件';
  });
});

window.addEventListener('beforeunload',function(){
  if(reportArtifactUrl){URL.revokeObjectURL(reportArtifactUrl);}
});

$('#btn-again').addEventListener('click',function(){location.reload();});
$('#btn-reset').addEventListener('click',function(){location.reload();});
$('#btn-history').addEventListener('click',function(){
  fetch('/api/history').then(function(r){return r.json();}).then(function(d){
    var tb=$('#hs-table tbody');tb.innerHTML='';
    (d.runs||[]).forEach(function(r){
      var tr=document.createElement('tr');
      tr.innerHTML='<td class="mono">'+r.run_id+'</td><td>'+r.started_at+'</td><td>'+r.rows+'</td><td>'+(r.issues||0)+' 个</td>';
      tb.appendChild(tr);
    });
    show('pg-history');
  });
});
$('#btn-back').addEventListener('click',function(){show('pg-intake');});
</script>
</body>
</html>"""


_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))')


def parse_multipart_file(body: bytes, content_type: str) -> tuple[str, bytes]:
    """P1-08：按 RFC 2046 边界精确切分 multipart，绝不对内容调用 strip()。

    旧实现用 find/rfind 定位 \r\n\r\n 再 `strip(b"\\r\\n")`——那会吃掉
    CSV 首尾合法换行（首行是表头、末行是数据），等于无痕迹地改了用户数据，
    且让 raw SHA-256 不能代表浏览器实际上传的字节。
    这里只剥离 multipart 自身的分隔符 CRLF，其余字节原样返回。
    """
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        raise ValueError("请求缺少 multipart boundary（Content-Type 不合法）")
    boundary = (m.group(1) or m.group(2)).encode("utf-8")
    delim = b"--" + boundary
    for part in body.split(delim)[1:]:
        if not part.startswith(b"\r\n"):
            continue                       # 起始 CRLF 属于分隔符，不是内容
        head, sep, rest = part[2:].partition(b"\r\n\r\n")
        if not sep:
            continue
        if rest.endswith(b"\r\n"):
            rest = rest[:-2]               # 仅剥离分隔符前的那个 CRLF
        fname = re.search(rb'filename="([^"]*)"', head)
        return (fname.group(1).decode("utf-8", "replace") if fname else "upload.csv", rest)
    raise ValueError("multipart 请求中没有找到文件字段")


class Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        # P1-05 ④：请求体超时——上传/长连接的 rfile.read 卡住时自动断开
        try:
            self.connection.settimeout(30)
        except Exception:  # noqa: BLE001
            pass

    def _rate_limited(self) -> bool:
        """P1-05 ④：每 IP 滑动窗口限速，超限 429。返回 True 表示已拒绝。"""
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
        """审计 #7 最小防护：本服务无会话/CSRF token，靠 Origin+Host 白名单
        挡掉跨站请求（CSRF）与 DNS rebinding。返回 True 表示已拒绝并回 403。
        同源 fetch：Host 为本机、Origin 缺失或等于本机 → 放行。"""
        host = self.headers.get("Host", "")
        if host and host.split(":", 1)[0] not in ("127.0.0.1", "localhost"):
            self._json({"error": "forbidden host"}, 403)
            return True
        origin = self.headers.get("Origin")
        if origin:
            allow = ("http://127.0.0.1:8765", "http://localhost:8765")
            if origin not in allow:
                self._json({"error": "cross-origin request blocked"}, 403)
                return True
        return False

    def _csrf_ok(self) -> bool:
        """P1-05 ③：变更操作（POST）必须带启动令牌（本机会话凭据）。"""
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
        # P1-05 ①：页面 JS 内嵌启动令牌，fetch 变更操作自动带上
        body = PAGE.replace("__CSRF_TOKEN__", _CSRF_TOKEN).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self._reject_cross_origin() or self._rate_limited():
            return
        if self.path in ("/", "/index.html"):
            self._page()
            return
        if self.path == "/api/session":
            # P1-05 ①：本机会话认证——客户端从这里取启动令牌
            self._json({"csrf_token": _CSRF_TOKEN})
            return
        if self.path.startswith("/api/checkup"):
            q = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            try:
                self._json(action_checkup(q))
            except KeyError as e:
                self._json({"error": _safe_error(e)}, 404)
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 400)
            return
        if self.path.startswith("/api/history"):
            self._json(action_history())
            return
        if self.path.startswith("/api/agent/keys"):
            self._json(action_agent_keys())
            return
        if self.path.startswith("/api/claims"):
            q = parse_qs(urlparse(self.path).query).get("run", [""])[0]
            try:
                self._json(action_claims(q))
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 400)
            return
        if self.path.startswith("/api/explain"):
            q = parse_qs(urlparse(self.path).query).get("run", [""])[0]
            try:
                self._json(action_explain(q))
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 400)
            return
        if self.path.startswith("/api/export"):
            q = parse_qs(urlparse(self.path).query).get("run", [""])[0]
            try:
                m = action_export(q)
                body = m["content"]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{quote(m['file_name'])}",
                )
                self.send_header("X-Export-Name", quote(m["file_name"]))
                self.send_header("X-Export-Path", quote(m["path"]))
                self.send_header("X-Export-Sha256", m["sha256"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 500)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self._reject_cross_origin() or self._rate_limited():
            return
        if not self._csrf_ok():       # P1-05 ③：POST 一律校验启动令牌
            return
        if self.path == "/api/upload":
            if not _UPLOAD_SEM.acquire(blocking=False):   # P1-05 ④：上传并发上限
                self._json({"error": "上传并发已达上限，请稍后重试"}, 429)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_UPLOAD + 4096:
                    self._json({"error": "文件超过 20MB 上限"}, 400)
                    return
                ctype = self.headers.get("Content-Type", "")
                body = self.rfile.read(length)
                # P1-08：标准边界解析，原始字节一个不动
                fname, content = parse_multipart_file(body, ctype)
                self._json(action_upload(fname, content))
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 400)
            finally:
                _UPLOAD_SEM.release()
            return
        if self.path.startswith("/api/run"):
            q = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            # P1-07 第 5 条：幂等键——重复请求返回同一任务，不重复建批次
            idem = self.headers.get("Idempotency-Key") or None
            try:
                self._json(action_run(q, idempotency_key=idem))
            except RunConflict as e:
                self._json({"error": _safe_error(e), "code": "run_in_progress"}, 409)
            except Exception as e:  # noqa: BLE001
                self._json(_pipeline_error_payload(e), 500)
            return
        if self.path.startswith("/api/agent"):
            # ADR-17 Capability Matrix 入口：per-key 白名单越界 → 403 + 审计
            from core.auth.tool_registry import PermissionDenied
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8") or "{}"
                req = json.loads(raw)
                self._json(action_agent(req))
            except PermissionDenied as e:
                self._json({"error": _safe_error(e), "code": "permission_denied"}, 403)
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 400)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        pass


def serve() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"TraceableMetrics 本地工作台 → {url}  （Ctrl+C 停止）")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
