"""D9 双轨备份与恢复演练。

备份是治理产物，不改变源卷；恢复只发布到全新的目标目录，避免覆盖现有数据。
所有文件通过 manifest 记录相对路径、大小和 SHA-256，校验失败时不发布恢复结果。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACKS = frozenset({"commerce", "research"})
MANIFEST_VERSION = 1


class BackupError(ValueError):
    """备份/恢复请求被拒绝。"""


def _validate_track(track: str) -> str:
    if track not in TRACKS:
        raise BackupError(f"非法轨道：{track!r}")
    return track


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _ensure_off_volume(path: Path, source: Path, label: str) -> None:
    if _inside(path, source):
        raise BackupError(f"{label} 必须位于源卷之外：{path}")


def _safe_rel(rel: str) -> Path:
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BackupError(f"manifest 含越界路径：{rel!r}")
    return candidate


def _files(source: Path) -> list[Path]:
    if not source.exists() or not source.is_dir():
        raise BackupError(f"源卷不存在或不是目录：{source}")
    return sorted(p for p in source.rglob("*") if p.is_file())


def _new_id(track: str) -> str:
    return f"bkp-{track}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def backup_volume(source_volume: Path, backup_root: Path, track: str) -> dict[str, Any]:
    """创建单轨不可覆盖备份，返回 manifest 元数据。"""
    track = _validate_track(track)
    source = Path(source_volume).resolve()
    root = Path(backup_root).resolve()
    _ensure_off_volume(root, source, "backup_root")
    files = _files(source)
    backup_id = _new_id(track)
    root.mkdir(parents=True, exist_ok=True)
    final_dir = root / track / backup_id
    if final_dir.exists():
        raise BackupError(f"备份 ID 冲突，拒绝覆盖：{backup_id}")
    staging = root / track / f".{backup_id}.staging"
    staging.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    try:
        for source_file in files:
            rel = source_file.relative_to(source)
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
            entries.append({
                "path": rel.as_posix(),
                "size": source_file.stat().st_size,
                "sha256": _sha256(source_file),
            })
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "backup_id": backup_id,
            "track": track,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(entries),
            "files": entries,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(final_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**manifest, "path": final_dir}


def _load_manifest(backup_dir: Path, track: str) -> dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"备份 manifest 无法读取：{manifest_path}") from exc
    if not isinstance(raw, dict) or raw.get("manifest_version") != MANIFEST_VERSION:
        raise BackupError("不支持的备份 manifest 版本")
    if raw.get("track") != track:
        raise BackupError(f"备份轨道不匹配：manifest={raw.get('track')!r}, requested={track!r}")
    if raw.get("file_count") != len(raw.get("files", [])):
        raise BackupError("备份 manifest file_count 不一致")
    return raw


def restore_backup(backup_dir: Path, restore_root: Path, track: str) -> dict[str, Any]:
    """校验并恢复单轨备份到新目录；目标存在或非空时拒绝。"""
    track = _validate_track(track)
    backup = Path(backup_dir).resolve()
    target = Path(restore_root).resolve()
    manifest = _load_manifest(backup, track)
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise BackupError(f"恢复目标必须是不存在或空目录，拒绝覆盖：{target}")
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.restore-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=False, exist_ok=False)
    try:
        for entry in manifest["files"]:
            rel = _safe_rel(entry.get("path", ""))
            source_file = backup / rel
            if not source_file.is_file():
                raise BackupError(f"备份文件缺失：{rel.as_posix()}")
            if _sha256(source_file) != entry.get("sha256"):
                raise BackupError(f"备份文件 SHA-256 不匹配：{rel.as_posix()}")
            if source_file.stat().st_size != entry.get("size"):
                raise BackupError(f"备份文件大小不匹配：{rel.as_posix()}")
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
        (staging / "restore_manifest.json").write_text(
            json.dumps({"backup_id": manifest["backup_id"], "track": track,
                        "restored_at": datetime.now(timezone.utc).isoformat(),
                        "files": manifest["files"]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"backup_id": manifest["backup_id"], "track": track,
            "restore_root": target, "file_count": manifest["file_count"]}


def run_restore_drill(source_volume: Path, backup_root: Path, restore_root: Path,
                      track: str) -> dict[str, Any]:
    """执行一次可复核的单轨恢复演练。"""
    backup = backup_volume(source_volume, backup_root, track)
    restored = restore_backup(backup["path"], restore_root, track)
    return {
        "ok": True,
        "track": track,
        "backup_id": backup["backup_id"],
        "file_count": backup["file_count"],
        "backup_root": backup["path"],
        "restore_root": restored["restore_root"],
    }


__all__ = ["BackupError", "backup_volume", "restore_backup", "run_restore_drill"]
