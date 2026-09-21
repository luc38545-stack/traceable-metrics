"""运行台账（附录 T 冻结项⑤）—— SQLite 表 + 结论对象完整性校验。

架构书要求：
- 台账为 SQLite 表（run_id / snapshot / provenance 可机查）
- 负向：结论对象缺 provenance 任一元 → 入库被拒（D10 已有，此处补台账断链场景）

台账库路径由调用方（app 层）注入；core 不硬编码任何 /data/* 路径（C-4）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

# 结论对象必须携带的溯源字段（D3：任一数字可答 snapshot × 不可变代码指纹 × run_id）。
# V1 仓库无 git，commit_sha 取源码树不可变指纹 build_digest()；
# batch_id 只是原始数据批次指纹，不能代替代码版本（审计 #5 新增严重项）。
REQUIRED_PROVENANCE = ("run_id", "snapshot_id", "batch_id", "commit_sha")

# 台账 JSON schema 版本（阶段2 · Skill 依赖接口）。
# 写入方（run_v1 / run_research）必须带此字段；消费方（core.copilot.gate 等）
# 只解析明确支持的版本，缺失或不支持一律显式终止，绝不猜测（P4 失败显性化）。
LEDGER_SCHEMA_VERSION = 1


RUNTIME_LOCK = "requirements.lock.txt"


def build_digest(root: Path | None = None) -> str:
    """不可变代码指纹（审计 #5）：对运行时源码树做整体 sha256。

    参与指纹：core/ plugins/ schemas/ infra/ dbt/ + requirements.lock.txt。
    任何一处源码或依赖锁改动 → 指纹变化 → 老 run 的 provenance 无法冒充新代码版本
    （可复现性判据：snapshot × commit × run_id 三要素缺一不可）。
    缓存策略：仅对默认真实根（root=None）做进程级缓存；显式注入 root 时现算
    （测试/审计场景要求每次读取反映当下文件状态）。
    失败语义（发布契约 RC5）：任一指纹输入缺失或不可读 → 显式抛错，绝不静默跳过。
    """
    if root is None:
        return _build_digest_real_root()
    return _build_digest_tree(Path(root).resolve())


@lru_cache(maxsize=1)
def _build_digest_real_root() -> str:
    return _build_digest_tree(Path(__file__).resolve().parents[2])


def _build_digest_tree(root: Path) -> str:
    h = hashlib.sha256()
    parts: list[Path] = []
    for sub in ("core", "plugins", "schemas", "infra", "dbt"):
        d = root / sub
        if d.exists():
            parts.extend(sorted(p for p in d.rglob("*") if p.is_file()))
    req = root / RUNTIME_LOCK
    if not req.exists():
        raise FileNotFoundError(
            f"代码指纹输入缺失：{req}——发布基线不完整，拒绝产出指纹")
    parts.append(req)
    for p in parts:
        h.update(p.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError as e:
            raise OSError(f"代码指纹读取失败：{p}") from e
        h.update(b"\0")
    return h.hexdigest()


def dependency_digest(root: Path | None = None) -> str:
    """P0-06：依赖锁摘要（requirements.lock.txt 指纹）。

    发布契约 RC1/RC2：只认 runtime lock；缺锁文件显式抛错，不返回 None。
    """
    resolved = Path(root).resolve() if root else Path(__file__).resolve().parents[2]
    return _dependency_digest_tree(resolved)


@lru_cache(maxsize=1)
def _dependency_digest_tree(root: Path) -> str:
    req = root / RUNTIME_LOCK
    if not req.exists():
        raise FileNotFoundError(
            f"依赖锁缺失：{req}——台账依赖摘要拒绝降级到宽松清单")
    return hashlib.sha256(req.read_bytes()).hexdigest()


def file_digest(path: Path) -> str:
    """P0-06：单个文件指纹（给 Metric Contract 等契约文件用）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_dirty(root: Path | None = None) -> str:
    """P0-06：工作区是否有未提交修改。

    有 .git → 用 `git status --porcelain` 判定，dirty 时明确标记不可作正式复现产物；
    git 判定不了（非零退出/超时/异常）→ 如实返回 unknown(...)，不伪装成 clean（发布契约 RC6）；
    无 .git（当前仓库形态）→ 如实返回 unknown，不伪装成 clean。
    """
    import subprocess

    root = Path(root).resolve() if root else Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return "unknown(no-git-repo)"
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root),
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 — 判定不了就如实说 unknown，不猜
        return "unknown(git-unavailable)"
    if out.returncode != 0:
        return f"unknown(git-exit-{out.returncode})"
    return "true" if out.stdout.strip() else "false"


class LedgerError(ValueError):
    """台账写入被拒。"""


def _conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path))
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id      TEXT PRIMARY KEY,
            track       TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            batch_id    TEXT,
            snapshot_id TEXT,
            rows        INTEGER,
            metric      TEXT,
            metric_version INTEGER,
            health      TEXT,
            provenance  TEXT NOT NULL,
            steps       TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS queries (
            query_id     TEXT PRIMARY KEY,
            run_id       TEXT,
            metric       TEXT NOT NULL,
            track        TEXT NOT NULL,
            ts           TEXT NOT NULL,
            via_semantic INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS exports (
            export_id    TEXT PRIMARY KEY,
            run_id       TEXT NOT NULL,
            track        TEXT NOT NULL,
            metric       TEXT,
            file_name    TEXT NOT NULL,
            dest         TEXT NOT NULL,
            sha256       TEXT NOT NULL,
            ts           TEXT NOT NULL
        )
        """
    )
    # P0-05：统一 append-only 事件流（run/query/export/授权拒绝/对账失败/失败 run）。
    # 状态变化只追加事件，绝不改动历史记录（CI R8 静态强制）。
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id     TEXT PRIMARY KEY,
            ts           TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            subject_id   TEXT,
            track        TEXT,
            payload      TEXT NOT NULL
        )
        """
    )
    _migrate(c)
    return c


def _migrate(c: sqlite3.Connection) -> None:
    """轻量迁移：老库补列（只 ADD COLUMN，绝不重写/删除任何历史记录）。"""
    have = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    if "snapshot_sha256" not in have:
        c.execute("ALTER TABLE runs ADD COLUMN snapshot_sha256 TEXT")


def write_run(db_path: Path, ledger: dict[str, Any]) -> None:
    """写一条运行台账。provenance 三元组不齐 → 拒收（负向用例）。"""
    prov = ledger.get("provenance") or {}
    missing = [k for k in REQUIRED_PROVENANCE if not prov.get(k)]
    if missing:
        raise LedgerError(
            f"conclusion rejected: provenance missing {missing} "
            f"(run {ledger.get('run_id')})"
        )
    c = _conn(db_path)
    try:
        # P0-05：台账必须真正 append-only——旧实现用覆盖式写入会静默改写
        # 已存在的 run_id，破坏「run 一旦入库不可篡改」的审计语义。
        # 重复 run_id 让主键约束抛 IntegrityError → 显式转 LedgerError 拒绝。
        try:
            c.execute(
                "INSERT INTO runs "
                "(run_id, track, started_at, batch_id, snapshot_id, rows, metric, "
                " metric_version, health, provenance, steps, snapshot_sha256) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ledger["run_id"],
                    ledger.get("track", ""),
                    ledger.get("started_at", ""),
                    ledger.get("batch_id"),
                    ledger.get("snapshot_id"),
                    ledger.get("rows"),
                    ledger.get("metric"),
                    ledger.get("metric_version"),
                    json.dumps(ledger.get("health_summary", {}), ensure_ascii=False),
                    json.dumps(prov, ensure_ascii=False),
                    json.dumps(ledger.get("steps", []), ensure_ascii=False),
                    ledger.get("snapshot_sha256"),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise LedgerError(
                f"run already recorded, append-only ledger rejects duplicate: "
                f"{ledger['run_id']}"
            ) from e
        c.commit()
    finally:
        c.close()
    # 成功 run 也进事件流（D8：审计全覆盖，成功/失败同样可追溯）
    log_event(
        db_path, "run_recorded", subject_id=ledger["run_id"],
        track=ledger.get("track"),
        payload={"snapshot_id": ledger.get("snapshot_id"),
                 "batch_id": ledger.get("batch_id"),
                 "metric": ledger.get("metric")},
    )


def write_run_failed(
    db_path: Path,
    run_id: str,
    track: str,
    stage: str,
    error_type: str,
    error: str,
    batch_id: str | None = None,
    started_at: str | None = None,
) -> None:
    """P0-03/P0-05：失败 run 也必须落账——记录失败阶段、错误类型和关联 batch。

    失败不写 runs 表（那张表只放成功产物），而是作为失败事件进 append-only 事件流，
    保证「成功台账 + 缺失快照」这种不一致状态不可能悄悄出现。
    """
    log_event(
        db_path, "run_failed", subject_id=run_id, track=track,
        payload={"stage": stage, "error_type": error_type, "error": error,
                 "batch_id": batch_id, "started_at": started_at},
    )


def log_event(
    db_path: Path,
    event_type: str,
    subject_id: str | None = None,
    track: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """统一 append-only 事件流（P0-05）：只 INSERT，不 UPDATE/DELETE（CI R8 强制）。"""
    event_id = f"e-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO events (event_id, ts, event_type, subject_id, track, payload) "
            "VALUES (?,?,?,?,?,?)",
            (event_id, datetime.now().isoformat(timespec="seconds"), event_type,
             subject_id, track,
             json.dumps(payload or {}, ensure_ascii=False, default=str)),
        )
        c.commit()
    finally:
        c.close()


def log_export(
    db_path: Path,
    export_id: str,
    run_id: str,
    track: str,
    metric: str | None,
    file_name: str,
    dest: str,
    sha256: str,
) -> None:
    """记录一次导出（D8 审计全覆盖：导出/分享动作必须进 append-only 审计流）。"""
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO exports (export_id, run_id, track, metric, file_name, dest, sha256, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (export_id, run_id, track, metric, file_name, dest, sha256,
             datetime.now().isoformat(timespec="seconds")),
        )
        c.commit()
    finally:
        c.close()
    log_event(db_path, "export", subject_id=export_id, track=track,
              payload={"run_id": run_id, "metric": metric, "file_name": file_name,
                       "sha256": sha256})


def log_query(
    db_path: Path,
    query_id: str,
    metric: str,
    track: str,
    run_id: str | None = None,
    via_semantic: bool = True,
) -> None:
    """记录一次取数（Semantic API 必记；绕过语义层的直连在网关层拒绝并审计）。"""
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO queries (query_id, run_id, metric, track, ts, via_semantic) "
            "VALUES (?,?,?,?,?,?)",
            (query_id, run_id, metric, track, datetime.now().isoformat(timespec="seconds"),
             1 if via_semantic else 0),
        )
        c.commit()
    finally:
        c.close()
    log_event(db_path, "query", subject_id=query_id, track=track,
              payload={"run_id": run_id, "metric": metric, "via_semantic": bool(via_semantic)})
