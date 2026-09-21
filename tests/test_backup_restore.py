"""D9 双轨备份、校验与恢复演练。"""
import json
from pathlib import Path

import pytest

from core.governance.backup import BackupError, backup_volume, restore_backup, run_restore_drill


def _volume(root: Path, track: str) -> Path:
    volume = root / f"{track}-source"
    (volume / "raw").mkdir(parents=True)
    (volume / f"{track}.db").write_bytes(f"{track}-ledger".encode())
    (volume / "raw" / "batch.csv").write_text(f"track,{track}\n", encoding="utf-8")
    return volume


def test_d9_two_track_restore_drill(tmp_path: Path):
    backup_root = tmp_path / "offsite-backups"
    commerce = run_restore_drill(_volume(tmp_path, "commerce"), backup_root, tmp_path / "commerce-restored", "commerce")
    research = run_restore_drill(_volume(tmp_path, "research"), backup_root, tmp_path / "research-restored", "research")
    assert commerce["ok"] and research["ok"]
    assert (tmp_path / "commerce-restored" / "commerce.db").read_text() == "commerce-ledger"
    assert (tmp_path / "research-restored" / "research.db").read_text() == "research-ledger"
    assert (backup_root / "commerce").is_dir() and (backup_root / "research").is_dir()


def test_d9_backup_root_must_be_off_volume(tmp_path: Path):
    volume = _volume(tmp_path, "commerce")
    with pytest.raises(BackupError, match="源卷之外"):
        backup_volume(volume, volume / "backups", "commerce")


def test_d9_cross_track_restore_rejected(tmp_path: Path):
    volume = _volume(tmp_path, "commerce")
    backup = backup_volume(volume, tmp_path / "backups", "commerce")
    with pytest.raises(BackupError, match="轨道不匹配"):
        restore_backup(backup["path"], tmp_path / "wrong", "research")


def test_d9_tampered_backup_is_rejected_and_target_not_published(tmp_path: Path):
    volume = _volume(tmp_path, "research")
    backup = backup_volume(volume, tmp_path / "backups", "research")
    payload = backup["path"] / "raw" / "batch.csv"
    payload.write_text("tampered\n", encoding="utf-8")
    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="SHA-256"):
        restore_backup(backup["path"], target, "research")
    assert not target.exists()


def test_d9_existing_target_is_never_overwritten(tmp_path: Path):
    volume = _volume(tmp_path, "commerce")
    backup = backup_volume(volume, tmp_path / "backups", "commerce")
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(BackupError, match="拒绝覆盖"):
        restore_backup(backup["path"], target, "commerce")
    assert (target / "keep.txt").read_text() == "keep"


def test_d9_manifest_path_traversal_rejected(tmp_path: Path):
    volume = _volume(tmp_path, "research")
    backup = backup_volume(volume, tmp_path / "backups", "research")
    manifest_path = backup["path"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape.txt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupError, match="越界路径"):
        restore_backup(backup["path"], tmp_path / "restored", "research")
