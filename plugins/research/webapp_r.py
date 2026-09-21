#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraceableMetrics 论文轨研究者界面（face=R · S3 架构书 §六「独立入口」）。

与 commerce 工作台（face=C，plugins/commerce/webapp.py）严格隔离：
  - 独立端口 8766（commerce=8765）、独立 CSRF 启动令牌；
  - 独立数据卷 ROOT/data/research（uploads/runs/snapshots/repro/exports）；
  - 独立 upload 索引（research/uploads/index.json，绝不与 commerce 共享）。

研究者主旅程（原型 V2 精神延续）：
  上传三件套（队列 CSV + 数据集清单 manifest + 预注册台账 prereg）
  → 选择预注册计划 plan_id（口径冻结在登记时）
  → 跑论文轨管道（复用 plugins.research.run_research.run_research_pipeline，
     计数来自数据本身，claims 重算对账，失败显性登记）
  → 报告预览（结论状态 / 组计数 / 对账 / DP 剩余 / 步骤）
  → 导出（Quarto [R].qmd / 复现包 zip，均带 sha256）

安全边界（与 face=C 一致，P1-05/P1-08/P1-03）：
  - 只监听 127.0.0.1；Origin/Host 白名单；变更操作必须带 CSRF 启动令牌；
  - 限速 + 上传并发上限 + 请求体超时；文件大小上限（CSV 20MB，文档 1MB）；
  - 上传原样落盘 + 原始字节 sha256 指纹 + TOCTOU 用前核验；
  - 上传索引只存相对文件名，损坏时显性拒绝（P4，不静默重置）；
  - 并发 run 拒绝（共享 DuckDB 目标，HTTP 409）；幂等键防重复建批次。

启动（仓库根目录）：
    python -c "import sys; sys.path.insert(0, r'<仓库根>'); import plugins.research.webapp_r as w; w.serve()"
浏览器：http://127.0.0.1:8766
"""
from __future__ import annotations

import hashlib
import html
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

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys_path_injected = False

HOST, PORT = "127.0.0.1", 8766          # face=R 独立端口（face=C 为 8765）
MAX_UPLOAD_CSV = 20 * 1024 * 1024        # 队列 CSV
MAX_UPLOAD_DOC = 1 * 1024 * 1024         # manifest / prereg
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-一-龥]+")

_state: dict = {"runs": [], "uploads": {}}

# 与 face=C 同精神：共享 DuckDB 目标，管道必须串行化（已有任务在跑 → 409）
_RUN_LOCK = threading.Lock()
# 幂等键：重复请求返回首次结果，不重复建批次（P1-07 第 5 条）
_IDEMPOTENCY: dict[str, dict] = {}

# 本机会话认证：独立启动令牌（绝不与 face=C 共用）
_CSRF_TOKEN = secrets.token_hex(16)
# 简单限速（每 IP 滑动窗口）+ 上传并发上限 + 请求体超时
_RATE: dict[str, list[float]] = {}
_RATE_WINDOW, _RATE_MAX = 60.0, 120
_UPLOAD_SEM = threading.BoundedSemaphore(2)
# 上传索引读写加锁（并发上传写 index.json 会互相覆盖）
_INDEX_LOCK = threading.Lock()


class RunConflict(RuntimeError):
    """本轨已有 run 在进行中——拒绝并发（HTTP 409）。"""


def _ensure_core() -> None:
    global sys_path_injected
    if not sys_path_injected:
        import sys
        sys.path.insert(0, str(ROOT))
        sys_path_injected = True


def _ctx():
    _ensure_core()
    from core.ingestion.context import TrackContext
    return TrackContext(track="research", volume_root=ROOT / "data" / "research")


# ---------- 上传索引（研究卷独立；路径运行时派生，跟随 ROOT —— C-4 精神） ----------

def _upload_dir() -> Path:
    return ROOT / "data" / "research" / "uploads"


def _index_path() -> Path:
    return _upload_dir() / "index.json"


def _sha_index_path() -> Path:
    return _upload_dir() / "sha.json"


def _safe_error(e: Exception) -> str:
    """错误响应不得把内部绝对路径和完整异常返回前端（P1-05 ⑤）。"""
    msg = str(e) or type(e).__name__
    msg = msg.replace(str(ROOT), "<repo>")
    msg = re.sub(r"[A-Za-z]:[\\/][^\s'\"]{1,120}", "<path>", msg)
    msg = re.sub(r"\\\\[^\\\s'\"]{1,120}", "<unc-path>", msg)
    return (msg or "内部错误")[:400]


def _persist_upload(uid: str, display: str, sha256: str, size: int,
                    kind: str, summary: dict | None) -> None:
    """P1-03：索引只存相对文件名 + 元数据（绝不保存任意绝对路径）。

    索引损坏 → 显性抛错（P4），禁止静默重置为空（那等于无痕丢了审计事实）。
    """
    with _INDEX_LOCK:
        try:
            idx_path = _index_path()
            if idx_path.exists():
                raw = idx_path.read_text(encoding="utf-8").strip()
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
            "kind": kind,
            "sha256": sha256,
            "size": size,
            "summary": summary,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        tmp = idx_path.with_name(idx_path.name + ".tmp")
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, idx_path)          # 原子替换：读方只会看到完整索引


def _assert_upload_index_healthy() -> None:
    """上传前先确认既有索引可读，避免内容校验遮蔽 P4 损坏状态。"""
    idx_path = _index_path()
    if not idx_path.exists():
        return
    try:
        raw = idx_path.read_text(encoding="utf-8").strip()
        idx = json.loads(raw) if raw else {}
        if not isinstance(idx, dict):
            raise ValueError("upload index corrupted: not a mapping")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"上传索引损坏，拒绝继续：{e}（P4：不静默重置）") from e


def _upload_sha(uid: str) -> str | None:
    try:
        idx_path = _index_path()
        if idx_path.exists():
            d = json.loads(idx_path.read_text(encoding="utf-8"))
            ent = d.get(uid)
            if isinstance(ent, dict) and ent.get("sha256"):
                return ent["sha256"]
        sha_path = _sha_index_path()
        if sha_path.exists():
            d = json.loads(sha_path.read_text(encoding="utf-8"))
            return d.get(uid)
    except Exception:  # noqa: BLE001
        return None
    return None


def _verify_upload_sha(uid: str, path: Path) -> None:
    """P1-03 ⑤ / P1-08 ③：使用前再次核验文件 SHA-256，防 TOCTOU 替换。"""
    want = _upload_sha(uid)
    if not want:
        return
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != want:
        raise ValueError(
            f"上传文件指纹不一致（{actual[:12]}… ≠ {want[:12]}…）——"
            f"磁盘文件已被改动，拒绝继续（P4/TOCTOU）"
        )


def _find_upload(uid: str) -> Path:
    """先查内存，再查磁盘索引——服务重启后旧 upload_id 依然可用。

    索引只允许相对文件名，读取后 resolve 到本轨卷内；若 index.json 被篡改成
    卷外路径，assert_inside_volume 直接抛 PermissionError。
    """
    p = _state["uploads"].get(uid)
    if p and Path(p).exists():
        return _ctx().assert_inside_volume(Path(p))
    idx_path = _index_path()
    if idx_path.exists():
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        ent = idx.get(uid)
        if ent:
            rel = ent["file"] if isinstance(ent, dict) else ent
            candidate = idx_path.parent / Path(rel).name
            if candidate.exists():
                return _ctx().assert_inside_volume(candidate)
    raise KeyError(f"unknown upload_id: {uid}")


def _read_upload(uid: str) -> tuple[str, bytes]:
    """读上传文件：指纹核验 + 返回 (相对文件名, 原始字节)。"""
    p = _find_upload(uid)
    _verify_upload_sha(uid, p)
    return p.name, p.read_bytes()


# ---------- 上传校验 ----------

def _validate_csv_bytes(content: bytes) -> dict:
    """P1-08 第 4 条：分行/列/字段长度分别做形态上限，越界即显性拒绝。"""
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
    rows, max_cols = 0, 0
    header: list[str] | None = None
    try:
        for row in reader:
            rows += 1
            if rows > 200_000:
                raise ValueError("文件超过 200000 行上限")
            if rows == 1:
                header = [cell.strip().lstrip("\ufeff") for cell in row]
            max_cols = max(max_cols, len(row))
            if max_cols > 1_000:
                raise ValueError("文件超过 1000 列上限")
            for cell in row:
                if len(cell) > 10_000:
                    raise ValueError("存在超过 10000 字符的超长字段")
    except _csv.Error as e:
        raise ValueError(f"CSV 解析失败：{e}") from e
    if rows < 2:
        raise ValueError("CSV 只有表头没有数据行——无法分析")
    columns = set(header or ())
    commerce_markers = {"订单编号", "amount_total", "amount_paid", "销售渠道"}
    if columns & commerce_markers:
        raise ValueError(
            "检测到订单 CSV，不能上传到研究轨；请上传包含 "
            "participant_id、arm、converted 的队列 CSV"
        )
    required = ("participant_id", "arm", "converted")
    missing = [name for name in required if name not in columns]
    if missing:
        raise ValueError(
            "队列 CSV 缺少必需字段：" + "、".join(missing)
            + "；最小字段为 participant_id、arm、converted"
        )
    return {"lines": rows, "columns": max_cols}


def _parse_doc(content: bytes, kind: str) -> dict:
    """解析 manifest（JSON/YAML）或 prereg（JSON），必须是 dict。"""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{kind} 文件不是 UTF-8 编码（{e.reason}）") from e
    try:
        if kind == "manifest":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{kind} 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{kind} 必须是 JSON/YAML 对象（mapping）")
    return data


def _manifest_summary(data: dict) -> dict:
    """研究数据集清单摘要（四要素 + IRB + privacy）。"""
    irb = data.get("irb") or {}
    return {
        "dataset_name": data.get("dataset_name"),
        "version": data.get("version"),
        "owner": data.get("owner"),
        "collection_method": data.get("collection_method"),
        "irb": {"approved": irb.get("approved"),
                "expires_at": irb.get("expires_at"),
                "approval_no": irb.get("approval_no")},
        "privacy": data.get("privacy") or {},
    }


def action_upload(kind: str, name: str, content: bytes) -> dict:
    if kind not in ("csv", "manifest", "prereg"):
        raise ValueError(f"未知上传类型: {kind!r}（必须是 csv/manifest/prereg）")
    _assert_upload_index_healthy()
    limit = MAX_UPLOAD_CSV if kind == "csv" else MAX_UPLOAD_DOC
    if len(content) > limit:
        raise ValueError(f"文件超过 {limit // (1024 * 1024)}MB 上限")
    if kind == "csv":
        if not name.lower().endswith((".csv", ".txt")):
            raise ValueError("队列数据只支持 CSV")
        shape = _validate_csv_bytes(content)
        summary = {"lines": shape["lines"], "columns": shape["columns"]}
    elif kind == "manifest":
        if not name.lower().endswith((".json", ".yaml", ".yml")):
            raise ValueError("数据集清单只支持 .json / .yaml")
        summary = _manifest_summary(_parse_doc(content, "manifest"))
    else:
        if not name.lower().endswith(".json"):
            raise ValueError("预注册台账只支持 .json（PreRegistrationLedger 产物）")
        data = _parse_doc(content, "prereg")
        records = data.get("records")
        if not isinstance(records, dict):
            raise ValueError("预注册台账缺少 records 映射（非 PreRegistrationLedger 产物）")
        summary = {"plan_count": len(records)}

    # 先算原始字节指纹（在任何写入之前），再原样落盘
    raw_sha = hashlib.sha256(content).hexdigest()
    safe = SAFE_NAME_RE.sub("_", name) or "upload"
    upload_dir = _upload_dir()
    upload_dir.mkdir(parents=True, exist_ok=True)
    p = upload_dir / f"{uuid.uuid4().hex[:8]}__{safe}"
    p.write_bytes(content)                   # 原样落盘，绝不 strip/normalize
    _state["uploads"][p.name] = str(p)
    try:
        _persist_upload(p.name, safe, raw_sha, len(content), kind, summary)
    except Exception:
        p.unlink(missing_ok=True)            # 索引失败不留孤儿文件
        _state["uploads"].pop(p.name, None)
        raise
    return {
        "upload_id": p.name,
        "name": safe,
        "kind": kind,
        "size": len(content),
        "sha256": raw_sha,
        "summary": summary,
    }


# ---------- 预注册 / 清单预览 ----------

def action_prereg_options(prereg_id: str) -> dict:
    """从预注册台账列出全部计划（plan_id + 口径），供前端选择。"""
    _ensure_core()
    from core.statistics.prereg import PreRegistrationLedger

    _, content = _read_upload(prereg_id)
    tmp = _upload_dir() / f"prereg-{uuid.uuid4().hex[:8]}.json"
    try:
        tmp.write_bytes(content)
        ledger = PreRegistrationLedger(tmp)
        records = [{
            "plan_id": p.get("plan_id"),
            "hypothesis": p.get("hypothesis"),
            "outcome": p.get("outcome"),
            "design": p.get("design"),
            "method": p.get("method"),
            "mde": p.get("mde"),
            "alpha": p.get("alpha"),
            "power": p.get("power"),
            "primary_metric": p.get("primary_metric"),
        } for p in ledger.all()]
    finally:
        tmp.unlink(missing_ok=True)
    if not records:
        raise ValueError("预注册台账为空——没有可执行的计划")
    return {"records": records}


def action_manifest_preview(manifest_id: str) -> dict:
    """清单前置信息（四要素 + IRB + privacy），供研究者确认。"""
    _, content = _read_upload(manifest_id)
    data = _parse_doc(content, "manifest")
    return _manifest_summary(data)


# ---------- 论文轨管道编排 ----------

def action_run(
    csv_id: str,
    manifest_id: str,
    prereg_id: str,
    plan_id: str,
    *,
    question: str = "",
    period: str = "",
    mde: float | None = None,
    dp_budget_path: Path | None = None,
    dp_epsilon: float = 0.1,
    idempotency_key: str | None = None,
) -> dict:
    """跑一次论文轨 run（复用 run_research_pipeline）。

    - 幂等键：重复请求返回首次结果，不重复建批次；
    - 并发：锁被占用 → RunConflict（HTTP 409）；
    - 计数永远来自数据本身（管道内 group_counts 从 dwd_cohort 折叠）；
    - 任何闸门失败 → 管道内部已 write_run_failed（P0-03），异常继续上抛。
    """
    if idempotency_key:
        done = _IDEMPOTENCY.get(idempotency_key)
        if done is not None:
            return done
    if not _RUN_LOCK.acquire(blocking=False):
        raise RunConflict(
            "本轨已有一个管道正在运行，共享 DuckDB 目标不支持并行。"
            "请等当前任务结束后重试。"
        )
    try:
        result = _run_pipeline(csv_id, manifest_id, prereg_id, plan_id,
                               question=question, period=period, mde=mde,
                               dp_budget_path=dp_budget_path,
                               dp_epsilon=dp_epsilon)
    finally:
        _RUN_LOCK.release()
    if idempotency_key:
        _IDEMPOTENCY[idempotency_key] = result
    return result


def _run_pipeline(csv_id: str, manifest_id: str, prereg_id: str, plan_id: str,
                  *, question: str, period: str, mde: float | None,
                  dp_budget_path: Path | None, dp_epsilon: float) -> dict:
    _ensure_core()
    from core.ingestion.context import TrackContext
    from core.statistics.prereg import PreRegistrationLedger
    from plugins.research.run_research import run_research_pipeline

    # 三件套必须真实存在（显性点名，W12）
    try:
        csv_path = _find_upload(csv_id)
        _verify_upload_sha(csv_id, csv_path)
    except KeyError as e:
        raise ValueError(f"队列 CSV 未找到：{e}") from e
    try:
        man_path = _find_upload(manifest_id)
        _verify_upload_sha(manifest_id, man_path)
    except KeyError as e:
        raise ValueError(f"数据集清单未找到：{e}") from e
    try:
        prereg_path = _find_upload(prereg_id)
        _verify_upload_sha(prereg_id, prereg_path)
    except KeyError as e:
        raise ValueError(f"预注册台账未找到：{e}") from e

    # 计划必须已登记（口径冻结在登记时，执行不得自行编造）
    plan = PreRegistrationLedger(prereg_path).get(plan_id)
    if not plan:
        raise ValueError(f"预注册计划 {plan_id!r} 未登记——执行必须绑定已登记计划")

    ctx = TrackContext(track="research",
                       volume_root=ROOT / "data" / "research")
    out = run_research_pipeline(
        ctx=ctx, raw_csv=csv_path, manifest_path=man_path,
        prereg_path=prereg_path, prereg_id=plan_id, plan=plan,
        outcome=plan.get("outcome", "binary"),
        design=plan.get("design", "rct"),
        method=plan.get("method"),
        question=question or plan.get("hypothesis", ""),
        period=period,
        mde=plan.get("mde") if mde is None else mde,
        alpha=plan.get("alpha", 0.05),
        power=plan.get("power", 0.8),
        dp_budget_path=dp_budget_path, dp_epsilon=dp_epsilon,
    )
    concl = out["conclusion"]
    method = (concl.get("method") or {}).get("registered_name", "")
    result = concl.get("result") or {}
    response = {
        "run_id": out["run_id"],
        "snapshot_id": out["snapshot_id"],
        "conclusion_status": concl.get("status"),
        "method": method,
        "effect": (result.get("effect") or {}).get("value"),
        "p_value": result.get("p_value"),
        "counts": list(out["counts"]),
        "reconcile_diff": (out["reconcile"] or {}).get("reconcile_diff"),
        "dp_remaining": out["dp_remaining"],
        "repro_package": str(out["repro_package"]),
        "qmd_path": str(out["qmd_path"]),
        "steps": out["steps"],
    }
    response["preview"] = _plain_report_preview({
        "conclusion_status": response["conclusion_status"],
        "metric": out.get("metric", "conversion_rate"),
        "counts": response["counts"],
        "reconcile": out.get("reconcile"),
        "prereg_id": plan_id,
    })
    return response


# ---------- 报告 / 历史 / 导出 ----------

def _runs_dir() -> Path:
    return ROOT / "data" / "research" / "runs"


def _read_run(run_id: str) -> dict:
    f = _runs_dir() / f"{run_id}.json"
    if not f.exists():
        raise ValueError(f"run not found: {run_id}")
    return json.loads(f.read_text(encoding="utf-8"))


def _plain_report_preview(run: dict) -> dict:
    """把已落台账的研究事实翻译成非统计用户可读的中文摘要。"""
    counts = list(run.get("counts") or [])
    title = "研究队列转化率对比" if run.get("metric") == "conversion_rate" else "研究结果预览"
    summary = "本次运行没有足够的组计数，暂不能生成比例摘要。"
    if len(counts) >= 4:
        a_converted, a_total, b_converted, b_total = counts[:4]
        if a_total and b_total:
            a_rate = float(a_converted) / float(a_total)
            b_rate = float(b_converted) / float(b_total)
            diff_pp = (a_rate - b_rate) * 100
            direction = "高" if diff_pp >= 0 else "低"
            summary = (
                f"数据中，a 组转化 {a_converted}/{a_total}（{a_rate:.2%}），"
                f"b 组转化 {b_converted}/{b_total}（{b_rate:.2%}）；"
                f"a 组比 b 组{direction} {abs(diff_pp):.2f} 个百分点。"
            )
    status = run.get("conclusion_status") or "UNKNOWN"
    if status == "GREEN":
        status_explanation = (
            "GREEN 表示预注册的分析步骤、数据质量检查和 claims 对账都通过；"
            "它说明当前数据支持这次预设比较，不代表结论永久适用于所有数据。"
        )
    elif status == "RED":
        status_explanation = "RED 表示本次分析未形成可使用的统计结论，请先查看失败步骤和数据问题。"
    else:
        status_explanation = f"当前状态为 {status}，请结合管道步骤判断能否使用。"
    reconcile = (run.get("reconcile") or {}).get("reconcile_diff")
    evidence = "数据核对：通过（clean）" if reconcile == "clean" else f"数据核对：{reconcile or '未完成'}"
    return {
        "title": title,
        "summary": summary,
        "status_explanation": status_explanation,
        "evidence": evidence,
        "prereg_id": run.get("prereg_id") or "未记录",
    }


def _render_report_html(run: dict) -> str:
    """生成报告预览成品 HTML；所有数字均来自已落盘 run 台账。"""
    preview = _plain_report_preview(run)
    esc = lambda value: html.escape(str(value), quote=True)
    counts = list(run.get("counts") or [])
    count_text = " / ".join(esc(v) for v in counts[:4]) or "—"
    claims = (run.get("reconcile") or {}).get("claims") or []
    effect = next((c.get("value") for c in claims if str(c.get("metric", "")).startswith("effect:")), None)
    p_value = next((c.get("value") for c in claims if str(c.get("metric", "")).startswith("p_value:")), None)
    effect_text = f"{float(effect) * 100:.2f} 个百分点" if effect is not None else "—"
    p_text = f"{float(p_value):.3g}" if p_value is not None else "—"
    status = esc(run.get("conclusion_status") or "UNKNOWN")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>研究结论报告（论文轨）</title>
<style>body{{margin:0;background:#f4f6f6;color:#16191c;font:15px/1.8 "Segoe UI","Microsoft YaHei",sans-serif}}main{{max-width:820px;margin:0 auto;background:#fff;padding:28px 32px;box-sizing:border-box}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:24px 0 6px;color:#3e464d}}.status{{color:#1c7c46;font-weight:700}}.lead{{font-size:17px}}.meta{{color:#5b656d;font-size:12px}}table{{width:100%;border-collapse:collapse}}td{{padding:7px 8px;border-bottom:1px solid #e8ebec}}td:first-child{{color:#5b656d}}</style></head>
<body><main><h1>{esc(preview['title'])}</h1><div class="meta">运行 {esc(run.get('run_id') or '—')} · 预注册计划 {esc(preview['prereg_id'])}</div>
<h2>结论状态</h2><div class="status">{status}</div><p>{esc(preview['status_explanation'])}</p>
<h2>结果摘要</h2><p class="lead">{esc(preview['summary'])}</p>
<table><tr><td>组计数（转化 / 总数）</td><td>{count_text}</td></tr><tr><td>组间差异</td><td>{esc(effect_text)}</td></tr><tr><td>p 值</td><td>{esc(p_text)}</td></tr><tr><td>数据核对</td><td>{esc(preview['evidence'])}</td></tr></table>
<h2>使用说明</h2><p>这份报告只描述本次已核对数据和预先登记的比较结果。需要复核时，请使用同一运行编号、快照和复现包。</p>
</main></body></html>"""


def action_report_html(run_id: str) -> str:
    """返回指定 run 的最终 HTML 预览内容，不触发新的统计计算。"""
    return _render_report_html(_read_run(run_id))


def action_report(run_id: str) -> dict:
    """报告预览：读台账回显（结论 / 计数 / 对账 / DP 剩余 / 步骤）。"""
    run = _read_run(run_id)
    return {
        "run_id": run_id,
        "started_at": run.get("started_at"),
        "rows": run.get("rows"),
        "conclusion_status": run.get("conclusion_status"),
        "metric": run.get("metric"),
        "counts": run.get("counts"),
        "reconcile_diff": (run.get("reconcile") or {}).get("reconcile_diff"),
        "prereg_id": run.get("prereg_id"),
        "dp_remaining": run.get("dp_remaining"),
        "steps": run.get("steps"),
        "preview": _plain_report_preview(run),
    }


def action_history() -> dict:
    items = []
    for f in sorted(_runs_dir().glob("r-*.json"), reverse=True)[:20]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            items.append({
                "run_id": d.get("run_id"), "started_at": d.get("started_at"),
                "rows": d.get("rows"),
                "status": d.get("conclusion_status"),
                "reconcile_diff": (d.get("reconcile") or {}).get("reconcile_diff"),
            })
        except Exception:  # noqa: BLE001
            continue
    return {"runs": items}


def _latest_export(exports_dir: Path, pattern: str, run_id: str) -> Path:
    """找 run 对应的导出物：优先名字含 run_id，否则取最新（同一卷内）。"""
    exports_dir.mkdir(parents=True, exist_ok=True)
    cands = sorted(exports_dir.glob(pattern), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    if not cands:
        raise ValueError(f"导出物不存在（{pattern}）——先运行管道")
    for c in cands:
        if run_id in c.name:
            return c
    return cands[0]


def action_export_qmd(run_id: str) -> dict:
    """导出 Quarto [R].qmd（数字走插值，发布已进审计 D8）。"""
    _read_run(run_id)                       # run 必须存在（显性 400）
    exports_dir = ROOT / "data" / "research" / "exports"
    qmd = _ctx().assert_inside_volume(
        _latest_export(exports_dir, "[[]R]*.qmd", run_id))
    content = qmd.read_bytes()
    return {"file_name": qmd.name, "content": content,
            "path": str(qmd), "sha256": hashlib.sha256(content).hexdigest()}


def action_export_repro(run_id: str) -> dict:
    """导出复现包 zip（raw/快照/预注册/清单/依赖/代码指纹全证据袋）。"""
    _read_run(run_id)
    repro_dir = ROOT / "data" / "research" / "repro"
    zip_path = _ctx().assert_inside_volume(_latest_export(repro_dir, "*.zip", run_id))
    content = zip_path.read_bytes()
    return {"file_name": zip_path.name, "content": content,
            "path": str(zip_path), "sha256": hashlib.sha256(content).hexdigest()}


# ---------- HTTP 层 ----------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TraceableMetrics · 研究轨（face=R）</title>
<style>
:root{--bg:#f4f6f6;--paper:#fcfcfc;--ink:#16191c;--ink2:#3e464d;--muted:#5b656d;
--line:#dde1e3;--soft:#e8ebec;--panel:#f1f3f3;--accent:#3a5ba0;--accent-soft:#e7ecf6;
--ok:#1c7c46;--warn:#9a6a12;--bad:#b53a2a;
--font:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;--mono:"Cascadia Code",Consolas,monospace;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--ink);font-family:var(--font);font-size:13.5px;line-height:1.65;}
.topbar{background:#141a26;color:#e8ecec;display:flex;align-items:center;gap:18px;padding:10px 18px;}
.logo{font-family:var(--mono);font-size:12px;letter-spacing:.12em;}
.logo b{color:#fff;}
.chip{margin-left:auto;font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:#9db4e8;border:1px solid rgba(255,255,255,.14);padding:3px 8px;}
.wrap{max-width:900px;margin:0 auto;padding:28px 22px 80px;}
h1{font-size:22px;font-weight:600;letter-spacing:-.01em;}
.sub{color:var(--muted);font-size:12.5px;margin:4px 0 18px;}
.card{background:var(--paper);border:1px solid var(--line);padding:16px 18px;margin-bottom:14px;}
.card h3{font-size:13.5px;font-weight:600;margin-bottom:8px;}
.drop{border:1.5px dashed var(--faint,var(--muted));padding:26px;text-align:center;cursor:pointer;color:var(--muted);}
.drop.over{border-color:var(--accent);background:var(--accent-soft);}
.drop .big{font-size:13.5px;font-weight:600;margin-top:6px;color:var(--ink);}
.btn{border:1px solid var(--line);background:var(--paper);color:var(--ink);padding:8px 16px;font:inherit;font-size:12.5px;cursor:pointer;border-radius:2px;}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
.btn:disabled{opacity:.45;cursor:not-allowed;}
.kv{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--soft);font-size:12.5px;color:var(--ink2);}
.kv b{font-family:var(--mono);font-weight:600;color:var(--ink);}
select,input[type=text]{border:1px solid var(--line);background:#fff;padding:7px 10px;font:inherit;font-size:12.5px;width:100%;}
label.f{display:block;font-size:11.5px;color:var(--muted);margin:10px 0 4px;}
.hidden{display:none;}
.metric-big{font-family:var(--mono);font-size:30px;font-weight:600;color:var(--accent);}
.tag-ok{color:var(--ok);font-weight:600;}
.tag-warn{color:var(--warn);font-weight:600;}
.tag-bad{color:var(--bad);font-weight:600;}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px;}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--soft);}
th{font-size:10.5px;letter-spacing:.05em;color:var(--muted);}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#141a26;color:#fff;padding:10px 18px;font-size:12.5px;border-radius:2px;opacity:0;transition:opacity .3s;z-index:50;}
.toast.show{opacity:1;}
details{border:1px solid var(--soft);background:var(--panel);padding:9px 13px;margin-top:10px;}
summary{cursor:pointer;font-size:12px;color:var(--muted);}
.diag{font-size:12px;color:var(--ink2);margin-top:7px;line-height:1.8;}
.preview-summary{font-size:15px;line-height:1.8;margin-top:4px;}
.preview-explain{color:var(--ink2);margin-top:8px;line-height:1.8;}
.preview-tabs{display:flex;gap:6px;margin:18px 0 10px;border-bottom:1px solid var(--line);}
.preview-tab{border:0;background:transparent;color:var(--muted);padding:8px 12px;font:inherit;font-size:12.5px;cursor:pointer;border-bottom:2px solid transparent;}
.preview-tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600;}
.report-frame{display:block;width:100%;height:min(720px,72vh);min-height:520px;border:1px solid var(--line);background:#fff;}
</style>
</head>
<body>
<div class="topbar"><span class="logo"><b>TRACEABLE</b> 研究轨</span><span class="chip">FACE·R · 本机运行 · 独立卷 data/research</span></div>
<div class="wrap">

<section id="pg-intake">
  <h1>研究数据投料</h1>
  <div class="sub">上传队列 CSV + 数据集清单 + 预注册台账 → 选择冻结计划 → 跑论文轨管道</div>
  <div class="card">
    <h3>① 队列 CSV（participant_id / arm / converted …）</h3>
    <div class="diag">每行一位参与者，不是订单 CSV。最小字段：participant_id、arm（a/b）、converted（0/1）。数据从哪里来：上传经过整理的研究队列数据；预先登记的计划会冻结分析口径，防止事后挑结果。</div>
    <div class="drop" id="drop"><div class="big">把 CSV 拖到这里，或点击选择文件</div><div class="hint">≤ 20MB · 原样落盘 · 计数将由管道从数据本身计算</div></div>
    <input type="file" id="file" accept=".csv,.txt" class="hidden">
    <div id="csv-state" class="diag" style="color:var(--ok);"></div>
  </div>
  <div class="card">
    <h3>② 数据集清单（四要素 + IRB）</h3>
    <input type="file" id="file-man" accept=".json,.yaml,.yml">
    <div id="man-state" class="diag"></div>
    <div id="man-preview"></div>
  </div>
  <div class="card">
    <h3>③ 预注册台账（PreRegistrationLedger 产物）</h3>
    <input type="file" id="file-pre" accept=".json">
    <div id="pre-state" class="diag"></div>
    <label class="f">选择预注册计划（口径冻结在登记时）</label>
    <select id="plan-select" disabled><option value="">— 上传预注册台账后可选 —</option></select>
  </div>
  <div class="card">
    <h3>研究参数</h3>
    <label class="f">研究问题（留空用计划假设）</label>
    <input type="text" id="in-question" placeholder="如：处理组转化率是否显著高于对照组">
    <label class="f">分析周期（可选）</label>
    <input type="text" id="in-period" placeholder="如：2026-08">
    <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;">
      <button class="btn primary" id="btn-run" disabled>运行论文轨管道 →</button>
      <button class="btn" id="btn-history">查看历史运行</button>
    </div>
  </div>
</section>

<section id="pg-report" class="hidden">
  <h1>论文轨报告</h1>
  <div class="sub" id="rp-meta">—</div>
  <div class="card">
    <h3>结论状态 <span id="rp-status" class="tag-ok"></span></h3>
    <div class="kv"><span>执行方法</span><b id="rp-method">—</b></div>
    <div class="kv"><span>组计数（数据本身）</span><b id="rp-counts">—</b></div>
    <div class="kv"><span>claims 对账</span><b id="rp-reconcile">—</b></div>
    <div class="kv"><span>DP 预算剩余</span><b id="rp-dp">—</b></div>
  </div>
  <div class="preview-tabs" role="tablist" aria-label="报告预览视图">
    <button class="preview-tab active" id="tab-result-preview" role="tab" aria-selected="true" aria-controls="rp-result-preview">结果预览</button>
    <button class="preview-tab" id="tab-report-preview" role="tab" aria-selected="false" aria-controls="rp-report-preview">报告预览</button>
  </div>
  <div id="rp-preview">
    <div class="card" id="rp-result-preview">
      <h3>结果预览</h3>
      <div class="diag">GREEN 表示预注册分析步骤和数据核对通过；下面的摘要会根据本次运行更新。</div>
      <div id="rp-preview-title"><b>—</b></div>
      <div id="rp-preview-summary" class="preview-summary">—</div>
      <div id="rp-preview-explanation" class="preview-explain">—</div>
      <div id="rp-preview-evidence" class="diag">—</div>
    </div>
  </div>
  <div class="card hidden" id="rp-report-preview">
    <h3>报告预览</h3>
    <div id="rp-report-status" class="diag" role="status" aria-live="polite">打开后显示最终 HTML 报告成品。</div>
    <iframe id="rp-report-frame" class="report-frame" title="论文轨最终报告预览"></iframe>
  </div>
  <div class="card">
    <h3>管道步骤</h3>
    <table><thead><tr><th>步骤</th><th>结果</th></tr></thead><tbody id="rp-steps"></tbody></table>
  </div>
  <div class="card" id="rp-export">
    <h3>导出（均可复现）</h3>
    <div style="display:flex;gap:10px;flex-wrap:wrap;">
      <button class="btn primary" id="btn-qmd">下载 Quarto 论文（.qmd）</button>
      <button class="btn" id="btn-repro">下载复现包（.zip）</button>
    </div>
    <div id="rp-export-note" class="diag" style="margin-top:8px;"></div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;">
    <button class="btn" id="btn-again">再投一份</button>
  </div>
</section>

<section id="pg-history" class="hidden">
  <h1>历史运行</h1>
  <div class="sub">每次运行的台账都在 data/research/runs/ 落盘，数字可回溯到 raw 批次</div>
  <div class="card"><table id="hs-table"><thead><tr><th>运行</th><th>时间</th><th>行数</th><th>状态</th><th>对账</th></tr></thead><tbody></tbody></table></div>
  <button class="btn" id="btn-back">返回投料</button>
</section>

</div>
<div class="toast" id="toast"></div>

<script>
var $=function(s){return document.querySelector(s);};
function toast(m){var t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(function(){t.classList.remove('show');},3200);}
function show(id){['pg-intake','pg-report','pg-history'].forEach(function(p){$('#'+p).classList.toggle('hidden',p!==id);});}
var CSRF_TOKEN="__CSRF_TOKEN__";
var uploads={};   // {csv, manifest, prereg}
var currentRun=null;

var dz=$('#drop'),fi=$('#file');
dz.addEventListener('click',function(){fi.click();});
dz.addEventListener('dragover',function(e){e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',function(){dz.classList.remove('over');});
dz.addEventListener('drop',function(e){e.preventDefault();dz.classList.remove('over');if(e.dataTransfer.files.length)upload('csv',e.dataTransfer.files[0]);});
fi.addEventListener('change',function(){if(fi.files.length)upload('csv',fi.files[0]);});
$('#file-man').addEventListener('change',function(){if(this.files.length)upload('manifest',this.files[0]);});
$('#file-pre').addEventListener('change',function(){if(this.files.length)upload('prereg',this.files[0]);});

function upload(kind,f){
  var fd=new FormData();fd.append('file',f,f.name);
  fetch('/api/upload?kind='+kind,{method:'POST',body:fd,headers:{'X-CSRF-Token':CSRF_TOKEN}}).then(function(r){return r.json();}).then(function(d){
    if(d.error){toast(d.error);return;}
    uploads[kind]=d.upload_id;
    if(kind==='csv'){ $('#csv-state').textContent='✓ '+d.name+' · '+d.summary.lines+' 行 × '+d.summary.columns+' 列 · 指纹 '+d.sha256.slice(0,12)+'…'; }
    if(kind==='manifest'){ renderManifest(d); }
    if(kind==='prereg'){ loadPlans(d.upload_id); }
    ready();
  }).catch(function(){toast('上传失败——服务是否在运行？');});
}

function renderManifest(d){
  var s=d.summary||{};
  var irb=s.irb||{};
  $('#man-state').textContent='✓ '+d.name;
  $('#man-preview').innerHTML='<div class="diag">数据集：'+(s.dataset_name||'—')+'@'+(s.version||'—')+' · 负责人 '+(s.owner||'—')
    +'<br>IRB：'+(irb.approved?'已批准':'未批准')+' · '+irb.approval_no+' · 有效期至 '+(irb.expires_at||'—')
    +'<br>隐私：准标识符 '+(JSON.stringify((s.privacy||{}).quasi_identifiers||[]))+' · k='+((s.privacy||{}).k||'—')+'</div>';
}

function loadPlans(preregId){
  fetch('/api/prereg_options?id='+encodeURIComponent(preregId)).then(function(r){return r.json();}).then(function(d){
    if(d.error){toast(d.error);return;}
    var sel=$('#plan-select');sel.innerHTML='';
    (d.records||[]).forEach(function(p){
      var o=document.createElement('option');o.value=p.plan_id;
      o.textContent=p.plan_id+' · '+p.method+' · mde='+p.mde+(p.hypothesis?' · '+p.hypothesis:'');
      sel.appendChild(o);
    });
    sel.disabled=false;
    $('#pre-state').textContent='✓ 台账含 '+d.records.length+' 份计划';
    ready();
  });
}

function ready(){
  var ok=uploads.csv&&uploads.manifest&&uploads.prereg&&$('#plan-select').value;
  $('#btn-run').disabled=!ok;
}

function switchPreviewTab(name){
  var report=name==='report';
  $('#rp-preview').classList.toggle('hidden',report);
  $('#rp-report-preview').classList.toggle('hidden',!report);
  $('#tab-result-preview').classList.toggle('active',!report);
  $('#tab-report-preview').classList.toggle('active',report);
  $('#tab-result-preview').setAttribute('aria-selected',report?'false':'true');
  $('#tab-report-preview').setAttribute('aria-selected',report?'true':'false');
  if(report && currentRun){
    var frame=$('#rp-report-frame');
    if(!frame.src || frame.src==='about:blank'){
      $('#rp-report-status').textContent='正在加载最终 HTML 报告…';
      frame.onload=function(){ $('#rp-report-status').textContent='下方为最终 HTML 报告成品；不影响直接下载 Quarto 和复现包。'; };
      frame.onerror=function(){ $('#rp-report-status').textContent='报告预览加载失败，请直接下载 Quarto 文件查看。'; };
      frame.src='/api/report/html?run='+encodeURIComponent(currentRun.run_id);
    }
  }
}

$('#tab-result-preview').addEventListener('click',function(){switchPreviewTab('result');});
$('#tab-report-preview').addEventListener('click',function(){switchPreviewTab('report');});

$('#plan-select').addEventListener('change',ready);

$('#btn-run').addEventListener('click',function(){
  var b=this;b.disabled=true;b.textContent='管道运行中…';
  var body=JSON.stringify({question:$('#in-question').value,period:$('#in-period').value});
  var url='/api/run?csv='+encodeURIComponent(uploads.csv)
    +'&manifest='+encodeURIComponent(uploads.manifest)
    +'&prereg='+encodeURIComponent(uploads.prereg)
    +'&plan='+encodeURIComponent($('#plan-select').value);
  fetch(url,{method:'POST',headers:{'X-CSRF-Token':CSRF_TOKEN,'Content-Type':'application/json'},body:body}).then(function(r){return r.json();}).then(function(d){
    b.disabled=false;b.textContent='运行论文轨管道 →';
    if(d.error){toast(d.error);if(d.code==='run_in_progress'){b.disabled=false;}return;}
    renderReport(d);
  }).catch(function(){toast('运行失败');b.disabled=false;b.textContent='运行论文轨管道 →';});
});

function renderReport(d){
  currentRun=d;
  switchPreviewTab('result');
  $('#rp-meta').textContent='run '+d.run_id+' · 快照 '+d.snapshot_id;
  $('#rp-status').textContent=d.conclusion_status==='GREEN'?'GREEN ✓':'✗ '+d.conclusion_status;
  $('#rp-status').className=d.conclusion_status==='GREEN'?'tag-ok':'tag-bad';
  $('#rp-method').textContent=d.method||'—';
  $('#rp-counts').textContent=JSON.stringify(d.counts||[]);
  $('#rp-reconcile').textContent=d.reconcile_diff==='clean'?'✓ clean':'✗ '+d.reconcile_diff;
  $('#rp-reconcile').className=d.reconcile_diff==='clean'?'tag-ok':'tag-bad';
  $('#rp-dp').textContent=(d.dp_remaining===null||d.dp_remaining===undefined)?'未启用':d.dp_remaining.toFixed(4);
  var pv=d.preview||{};
  $('#rp-preview-title').textContent=pv.title||'研究结果预览';
  $('#rp-preview-summary').textContent=pv.summary||'暂无可读摘要';
  $('#rp-preview-explanation').textContent=pv.status_explanation||'请查看管道步骤和对账结果。';
  $('#rp-preview-evidence').textContent=(pv.evidence||'数据核对：未完成')+' · 预注册计划：'+(pv.prereg_id||'未记录');
  var tb=$('#rp-steps');tb.innerHTML='';
  (d.steps||[]).forEach(function(s){
    var tr=document.createElement('tr');
    tr.innerHTML='<td>'+s.step+'</td><td class="'+(s.ok?'tag-ok':'tag-bad')+'">'+(s.ok?'✓':'✗ '+(s.detail||''))+'</td>';
    tb.appendChild(tr);
  });
  $('#rp-export-note').textContent='Quarto：'+d.qmd_path+'；复现包：'+d.repro_package;
  show('pg-report');
  window.scrollTo(0,0);
}

function download(path,btn,label){
  var b=$(btn);b.disabled=true;b.textContent='生成中…';
  fetch(path).then(function(r){
    if(!r.ok){throw new Error('服务端拒绝');}
    var name=decodeURIComponent(r.headers.get('X-Export-Name')||'download');
    var sha=r.headers.get('X-Export-Sha256')||'';
    return r.blob().then(function(bl){return {name:name,sha:sha,blob:bl};});
  }).then(function(o){
    var url=URL.createObjectURL(o.blob),a=document.createElement('a');
    a.href=url;a.download=o.name;document.body.appendChild(a);a.click();
    document.body.removeChild(a);URL.revokeObjectURL(url);
    $('#rp-export-note').textContent='已下载 '+o.name+' · 指纹 '+o.sha.slice(0,16)+'…';
    toast('已下载');
    b.disabled=false;b.textContent=label;
  }).catch(function(e){toast('导出失败：'+e.message);b.disabled=false;b.textContent=label;});
}
$('#btn-qmd').addEventListener('click',function(){if(currentRun)download('/api/export/qmd?run='+encodeURIComponent(currentRun.run_id),'#btn-qmd','下载 Quarto 论文（.qmd）');});
$('#btn-repro').addEventListener('click',function(){if(currentRun)download('/api/export/repro?run='+encodeURIComponent(currentRun.run_id),'#btn-repro','下载复现包（.zip）');});

$('#btn-again').addEventListener('click',function(){location.reload();});
$('#btn-history').addEventListener('click',function(){
  fetch('/api/history').then(function(r){return r.json();}).then(function(d){
    var tb=$('#hs-table tbody');tb.innerHTML='';
    (d.runs||[]).forEach(function(r){
      var tr=document.createElement('tr');
      tr.innerHTML='<td class="mono">'+r.run_id+'</td><td>'+r.started_at+'</td><td>'+r.rows+'</td><td>'+(r.status||'—')+'</td><td>'+(r.reconcile_diff||'—')+'</td>';
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
    """按 RFC 2046 边界精确切分 multipart，绝不对内容调用 strip()。

    只剥离 multipart 自身的分隔符 CRLF，其余字节原样返回（P1-08）。
    """
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        raise ValueError("请求缺少 multipart boundary（Content-Type 不合法）")
    boundary = (m.group(1) or m.group(2)).encode("utf-8")
    delim = b"--" + boundary
    for part in body.split(delim)[1:]:
        if not part.startswith(b"\r\n"):
            continue
        head, sep, rest = part[2:].partition(b"\r\n\r\n")
        if not sep:
            continue
        if rest.endswith(b"\r\n"):
            rest = rest[:-2]
        fname = re.search(rb'filename="([^"]*)"', head)
        return (fname.group(1).decode("utf-8", "replace") if fname else "upload", rest)
    raise ValueError("multipart 请求中没有找到文件字段")


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
        """Host/Origin 白名单：挡跨站请求（CSRF）与 DNS rebinding。"""
        host = self.headers.get("Host", "")
        if host and host.split(":", 1)[0] not in ("127.0.0.1", "localhost"):
            self._json({"error": "forbidden host"}, 403)
            return True
        origin = self.headers.get("Origin")
        if origin:
            allow = ("http://127.0.0.1:8766", "http://localhost:8766")
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

    def _download(self, m: dict, ctype: str = "application/octet-stream") -> None:
        body = m["content"]
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(m['file_name'])}")
        self.send_header("X-Export-Name", quote(m["file_name"]))
        self.send_header("X-Export-Path", quote(m["path"]))
        self.send_header("X-Export-Sha256", m["sha256"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self._reject_cross_origin() or self._rate_limited():
            return
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path in ("/", "/index.html"):
            self._page()
            return
        if path == "/api/session":
            self._json({"csrf_token": _CSRF_TOKEN})
            return
        try:
            if path == "/api/prereg_options":
                self._json(action_prereg_options(q.get("id", [""])[0]))
                return
            if path == "/api/manifest_preview":
                self._json(action_manifest_preview(q.get("id", [""])[0]))
                return
            if path == "/api/report":
                self._json(action_report(q.get("run", [""])[0]))
                return
            if path == "/api/report/html":
                body = action_report_html(q.get("run", [""])[0]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/history":
                self._json(action_history())
                return
            if path == "/api/export/qmd":
                m = action_export_qmd(q.get("run", [""])[0])
                self._download(m, "text/markdown")
                return
            if path == "/api/export/repro":
                m = action_export_repro(q.get("run", [""])[0])
                self._download(m)
                return
        except KeyError:
            self._json({"error": "missing required parameter"}, 400)
            return
        except Exception as e:  # noqa: BLE001
            self._json({"error": _safe_error(e)}, 400)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self._reject_cross_origin() or self._rate_limited():
            return
        if not self._csrf_ok():
            return
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path == "/api/upload":
            if not _UPLOAD_SEM.acquire(blocking=False):
                self._json({"error": "上传并发已达上限，请稍后重试"}, 429)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                kind = q.get("kind", [""])[0]
                limit = MAX_UPLOAD_CSV if kind == "csv" else MAX_UPLOAD_DOC
                if length > limit + 4096:
                    self._json({"error": f"文件超过 {limit // (1024 * 1024)}MB 上限"}, 400)
                    return
                ctype = self.headers.get("Content-Type", "")
                body = self.rfile.read(length)
                fname, content = parse_multipart_file(body, ctype)
                self._json(action_upload(kind, fname, content))
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 400)
            finally:
                _UPLOAD_SEM.release()
            return
        if path == "/api/run":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8") or "{}"
                body = json.loads(raw) if raw.strip() else {}
            except Exception:  # noqa: BLE001
                body = {}
            idem = self.headers.get("Idempotency-Key") or None
            try:
                mde = body.get("mde")
                res = action_run(
                    q.get("csv", [""])[0], q.get("manifest", [""])[0],
                    q.get("prereg", [""])[0], q.get("plan", [""])[0],
                    question=str(body.get("question") or ""),
                    period=str(body.get("period") or ""),
                    mde=float(mde) if mde not in (None, "") else None,
                    dp_epsilon=float(body.get("dp_epsilon") or 0.1),
                    idempotency_key=idem,
                )
                self._json(res)
            except RunConflict as e:
                self._json({"error": _safe_error(e), "code": "run_in_progress"}, 409)
            except Exception as e:  # noqa: BLE001
                self._json({"error": _safe_error(e)}, 500)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        pass


def serve() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"TraceableMetrics 研究轨工作台（face=R）→ {url}  （Ctrl+C 停止）")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")


if __name__ == "__main__":
    serve()
