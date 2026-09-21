"""跨轨导出桥五闸（S4 双轨合龙 · 宪法 C-4「跨轨数据流动仅导出桥一条边」）。

设计依据（架构书原文未在本机留存，按审计文档引用的冻结条目施工，争议点坐标注）：
- 修改意见 P1-01：导出桥采用「源轨出口网关 → 受控中转 → 目标轨入口网关」三段式，
  不得给任一普通服务挂双网双卷；
- 修改意见 P1-04：导出桥只能由**人审控制面**（face=A）触发——token 即人审批准凭证；
- D8：跨轨桥动作必须进两侧台账的 append-only 审计流（先审计后交付）；
- ADR-18：导出物必须携带 manifest（来源 run/snapshot/batch/commit + 文件 sha256 +
  claims 摘要），目标轨加载前校验完整性；
- ADR-15：face=A 会话 15 分钟超时 → 人审 token 默认 TTL 15 分钟、一次性。

五闸（每闸独立可测，任一失败 → BridgeGateError 显性拒绝，绝不静默降级）：
  闸1 人审授权：redeem_transfer_token —— 无/伪造/过期/已用/purpose 不符 → 拒
  闸2 源轨出口：run 台账四元组 + 快照存在 + sha256 与台账一致 → 否则拒
  闸3 受控中转：快照复制进**非轨卷**中转目录（落在任一侧轨卷内 → 拒，防绕过）
  闸4 目标轨入口：复制进目标轨 imports/（assert_inside_volume 越界即炸）
                + 复制后 sha256 复核 + manifest 落盘（缺一 → 拒，不留半成品）
  闸5 双端审计：源/目标两侧 ledger 各写 export_bridge 事件，审计失败 → 拒交付

反向路径不存在性：目标轨 ctx.assert_inside_volume(源轨路径) 直接 PermissionError
（track 卷守卫是运行时唯一通道）；静态侧由 R 门禁保证普通插件零跨轨引用。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.ingestion.context import TrackContext

# 导出桥审计事件名（两侧统一；D8：跨轨桥动作全覆盖）
EVENT_BRIDGE = "export_bridge"


class BridgeGateError(RuntimeError):
    """导出桥某道闸门拒绝（P4：失败显性，绝不静默降级）。"""


# ---------------------------------------------------------------- 闸1：人审 token

@dataclass
class TransferToken:
    """face=A 人审批准的一次性导出凭证（ADR-15：TTL 默认 15 分钟）。"""

    token_id: str
    purpose: str            # "commerce→research" / "research→commerce"
    source_track: str       # 源轨（只允许从源轨卷读）
    target_track: str       # 目标轨（只允许写入目标轨卷）
    issued_at: str
    expires_at: str
    used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "purpose": self.purpose,
            "source_track": self.source_track,
            "target_track": self.target_track,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "used": self.used,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TransferToken":
        return cls(**d)

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        exp = datetime.fromisoformat(self.expires_at)
        return now >= exp


def _now() -> datetime:
    return datetime.now()


def issue_transfer_token(
    bridge_dir: Path,
    *,
    purpose: str,
    source_track: str,
    target_track: str,
    ttl_minutes: int = 15,
) -> TransferToken:
    """闸1·签发：face=A 人审批准后签发一次性 token（写入受控中转目录）。"""
    if source_track not in ("commerce", "research"):
        raise BridgeGateError(f"非法源轨: {source_track!r}")
    if target_track not in ("commerce", "research"):
        raise BridgeGateError(f"非法目标轨: {target_track!r}")
    if source_track == target_track:
        raise BridgeGateError("源轨与目标轨必须不同（同轨导出不需要导出桥）")
    if ttl_minutes <= 0:
        raise BridgeGateError("TTL 必须为正（默认 15 分钟，ADR-15）")

    now = _now()
    tok = TransferToken(
        token_id=f"tk-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
        purpose=purpose,
        source_track=source_track,
        target_track=target_track,
        issued_at=now.isoformat(timespec="seconds"),
        expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds"),
    )
    tok_dir = bridge_dir / "tokens"
    tok_dir.mkdir(parents=True, exist_ok=True)
    dest = tok_dir / f"{tok.token_id}.json"
    # 原子写：临时文件 + rename（防半截 token）
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok.to_dict(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, dest)
    return tok


def redeem_transfer_token(
    bridge_dir: Path,
    token_id: str,
    *,
    purpose: str,
    source_track: str,
    target_track: str,
) -> TransferToken:
    """闸1·核验：一次性消费 token。

    无 / 伪造 / 过期 / 已用 / purpose 或轨向不符 → BridgeGateError。
    校验通过即标记 used（一次性）：先读后写，写失败即拒绝（不留可复用凭证）。
    """
    if not token_id:
        raise BridgeGateError("缺少人审 token（导出桥必须由 face=A 控制面触发）")
    p = bridge_dir / "tokens" / f"{token_id}.json"
    if not p.exists():
        raise BridgeGateError(f"人审 token 无效或不存在: {token_id}")
    try:
        tok = TransferToken.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        raise BridgeGateError(f"人审 token 损坏: {e}") from e

    if tok.expired():
        raise BridgeGateError(f"人审 token 已过期（ADR-15 会话超时，请重新批准）")
    if tok.used:
        raise BridgeGateError("人审 token 已使用（一次性凭证，禁止重放）")
    if tok.purpose != purpose:
        raise BridgeGateError(
            f"token purpose 不符: {tok.purpose} != {purpose}（人审批准与请求不一致）")
    if tok.source_track != source_track or tok.target_track != target_track:
        raise BridgeGateError(
            f"token 轨向不符: {tok.source_track}→{tok.target_track} 与请求 "
            f"{source_track}→{target_track} 不一致")

    tok.used = True
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok.to_dict(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return tok


# ---------------------------------------------------------------- 闸2：源轨出口

def _read_source_run(source_ledger_db: Path, run_id: str) -> dict[str, Any]:
    """从源轨台账读 run 记录 + provenance（read-only）。无此 run → 拒。"""
    if not source_ledger_db.exists():
        raise BridgeGateError(f"源轨台账缺失: {source_ledger_db}")
    try:
        conn = sqlite3.connect(f"file:{source_ledger_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT track, snapshot_id, batch_id, provenance FROM runs "
                "WHERE run_id=?", (run_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise BridgeGateError(f"源轨台账不可读: {e}") from e
    if row is None:
        raise BridgeGateError(f"源轨台账无此 run: {run_id}（禁止导出未登记产物）")
    track, snapshot_id, batch_id, provenance_json = row
    prov = json.loads(provenance_json)
    return {
        "run_id": run_id,
        "track": track,
        "snapshot_id": snapshot_id,
        "batch_id": batch_id,
        "provenance": prov,
    }


def _verify_snapshot(snapshot_dir: Path, snapshot_id: str, expect_sha: str) -> Path:
    """闸2：快照必须存在且 sha256 与台账一致（篡改/缺失 → 拒）。"""
    if not snapshot_id:
        raise BridgeGateError("源 run 无快照记录（禁止导出未发布产物）")
    snap = snapshot_dir / snapshot_id
    if not snap.exists():
        raise BridgeGateError(f"源快照缺失: {snap}")
    actual = hashlib.sha256(snap.read_bytes()).hexdigest()
    if actual != expect_sha:
        raise BridgeGateError(
            f"源快照 sha256 与台账不一致（快照被篡改或台账失真），"
            f"actual={actual[:12]}… expect={expect_sha[:12]}…")
    return snap


# ---------------------------------------------------------------- 闸3：受控中转

def _assert_bridge_dir_neutral(bridge_dir: Path, source_ctx: TrackContext,
                               target_ctx: TrackContext) -> None:
    """闸3：受控中转目录不得落在任一侧轨卷内（防绕过卷守卫）。"""
    b = bridge_dir.resolve()
    for ctx, name in ((source_ctx, "源轨"), (target_ctx, "目标轨")):
        root = ctx.volume_root.resolve()
        if root in b.parents or b == root:
            raise BridgeGateError(
                f"受控中转目录不得位于{name}卷内（必须是非轨卷的受控区）: {b}")


# ---------------------------------------------------------------- 闸4：目标轨入口

def _import_into_target(target_ctx: TrackContext, bridge_dir: Path,
                        file_name: str, expect_sha: str) -> Path:
    """闸4：中转文件复制进目标轨 imports/（卷守卫 + sha256 复核 + manifest）。"""
    imports_dir = target_ctx.volume_root / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    src = bridge_dir / file_name
    if not src.exists():
        raise BridgeGateError(f"受控中转缺少文件: {file_name}")
    # 目标路径必须过 assert_inside_volume（越界即 PermissionError → 显性拒绝）
    dest = target_ctx.assert_inside_volume(imports_dir / file_name)
    if dest.exists():
        raise BridgeGateError(f"目标 imports 已存在同名文件（防覆盖）: {dest.name}")
    shutil.copy2(src, dest)
    actual = hashlib.sha256(dest.read_bytes()).hexdigest()
    if actual != expect_sha:
        dest.unlink(missing_ok=True)  # 半成品不留（P4）
        raise BridgeGateError(
            f"目标落卷后 sha256 复核失败（复制损坏）：{dest.name}")
    return dest


# ---------------------------------------------------------------- 主流程

def export_snapshot(
    *,
    token_id: str,
    purpose: str,
    source_ctx: TrackContext,
    target_ctx: TrackContext,
    bridge_dir: Path,
    source_ledger_db: Path,
    target_ledger_db: Path,
    run_id: str,
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """导出桥五闸串行：任何一闸失败 → BridgeGateError（显性，不静默）。

    claims：源 run 的 claims 摘要（ADR-18：导出物完整性随 manifest 携带），
    由调用方从源 run 台账/结论取；不传则 manifest 记 claims=[]（如实）。
    """
    bridge_dir = bridge_dir.resolve()

    # ---- 闸1：人审授权（face=A 签发的一次性 token）
    tok = redeem_transfer_token(
        bridge_dir, token_id, purpose=purpose,
        source_track=source_ctx.track, target_track=target_ctx.track)

    # ---- 闸2：源轨出口（台账 + 快照完整性）
    src = _read_source_run(source_ledger_db, run_id)
    if src["track"] != source_ctx.track:
        raise BridgeGateError(
            f"源 run 轨向不符: 台账 track={src['track']} 但调用方声明 {source_ctx.track}")
    snap_sha = src["provenance"].get("snapshot_sha256") or src.get("snapshot_sha256")
    if not snap_sha:
        # 兜底：老台账可能未写 snapshot_sha256（如实拒绝，不猜）
        raise BridgeGateError("源台账无 snapshot_sha256（无法核验快照完整性，拒绝导出）")
    snapshot = _verify_snapshot(source_ctx.snapshots_dir, src["snapshot_id"], snap_sha)

    # ---- 闸3：受控中转（非轨卷区）
    _assert_bridge_dir_neutral(bridge_dir, source_ctx, target_ctx)
    staging = bridge_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    file_name = f"{run_id.removeprefix('r-')}.snapshot.db"
    staged = staging / file_name
    staged.unlink(missing_ok=True)
    shutil.copy2(snapshot, staged)
    staged_sha = hashlib.sha256(staged.read_bytes()).hexdigest()
    if staged_sha != snap_sha:
        raise BridgeGateError("中转复制 sha256 不一致（复制损坏），拒绝继续")

    # ---- 闸4：目标轨入口（卷守卫 + 复核 + manifest）
    imported = _import_into_target(target_ctx, staging, file_name, snap_sha)
    manifest = {
        "export_id": f"eb-{_now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
        "purpose": purpose,
        "source": {
            "track": src["track"],
            "run_id": src["run_id"],
            "snapshot_id": src["snapshot_id"],
            "batch_id": src["batch_id"],
            "commit_sha": src["provenance"].get("commit_sha"),
        },
        "target": {"track": target_ctx.track},
        "file": {"name": file_name, "sha256": snap_sha, "size": imported.stat().st_size},
        "claims_count": len(claims or []),
        "claims": claims or [],
        "issued_at": tok.issued_at,
        "approved_by": "face=A 人审控制面",
        "exported_at": _now().isoformat(timespec="seconds"),
    }
    manifest_path = imports_dir = target_ctx.volume_root / "imports" / f"{file_name}.manifest.json"
    # manifest 落盘前再次确认在目标卷内
    manifest_path = target_ctx.assert_inside_volume(manifest_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 闸5：双端审计（D8：跨轨桥全覆盖；先审计后交付）
    try:
        from core.audit import ledger as _ledger

        _ledger.log_event(source_ledger_db, EVENT_BRIDGE, subject_id=manifest["export_id"],
                          track=source_ctx.track,
                          payload={"direction": "out", "target_track": target_ctx.track,
                                   "run_id": src["run_id"], "snapshot_id": src["snapshot_id"],
                                   "file": file_name, "sha256": snap_sha,
                                   "approved_by": "face=A 人审控制面"})
        _ledger.log_event(target_ledger_db, EVENT_BRIDGE, subject_id=manifest["export_id"],
                          track=target_ctx.track,
                          payload={"direction": "in", "source_track": source_ctx.track,
                                   "run_id": src["run_id"], "snapshot_id": src["snapshot_id"],
                                   "file": file_name, "sha256": snap_sha})
    except Exception as e:  # noqa: BLE001 — 审计失败即拒交付（先审计后交付）
        imported.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        raise BridgeGateError(f"双端审计失败，拒绝交付（已回滚目标卷）: {e}") from e

    # 交付成功后清理中转暂存（原始快照仍在源轨卷，不受影响）
    staged.unlink(missing_ok=True)

    return {
        "export_id": manifest["export_id"],
        "manifest": manifest,
        "imported": str(imported),
        "manifest_path": str(manifest_path),
        "sha256": snap_sha,
    }
