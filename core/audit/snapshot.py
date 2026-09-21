"""快照发布器（P0-03 · 审计整改施工意见）。

每个 run 精确对应唯一、永不覆盖的快照：

1. 快照目录 `exist_ok=False` 排他创建——同一 snapshot_id 重跑即失败；
2. 发布流程：复制到临时文件 → 计算 SHA-256 → 原子 rename 到最终路径
   （避免出现「半个快照」被后续对账读到）；
3. 最后才写 run 台账：确保不存在「成功台账 + 缺失快照」的不一致状态；
4. 任一步失败由调用方登记失败 run（见 `ledger.write_run_failed`）。

快照根目录由调用方注入（C-4：core 不选择数据位置）。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any


class SnapshotError(RuntimeError):
    """快照发布失败（重复/破坏/不可写）。"""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def publish_snapshot(
    db_path: Path,
    snapshots_root: Path,
    snapshot_id: str,
    track: str,
) -> dict[str, Any]:
    """把一个 DuckDB 库文件发布为不可变快照。

    db_path 必须先完成 CHECKPOINT（由调用方负责），复制的是磁盘上的完整字节。
    返回 {"snapshot_id", "path", "sha256", "size"}；失败抛 SnapshotError。
    """
    if not snapshot_id:
        raise SnapshotError("snapshot_id 为空——快照必须绑定具体 run")
    if not Path(db_path).exists():
        raise SnapshotError(f"源库文件不存在：{Path(db_path).name}")

    snap_dir = Path(snapshots_root) / snapshot_id
    try:
        snap_dir.mkdir(parents=True, exist_ok=False)   # 排他：同 id 重跑直接失败
    except FileExistsError as e:
        raise SnapshotError(
            f"快照已存在，拒绝覆盖：{snapshot_id}（每个 run 必须对应唯一快照）"
        ) from e

    final = snap_dir / f"{track}.db"
    tmp = snap_dir / f"{track}.db.partial"
    try:
        shutil.copy2(db_path, tmp)
        sha = _sha256(tmp)
        tmp.replace(final)          # 原子 rename：对账只会看到完整快照
    except Exception as e:  # noqa: BLE001 — 失败不留残骸，交调用方登记失败 run
        for junk in (tmp, final):
            try:
                junk.unlink()
            except OSError:
                pass
        try:
            snap_dir.rmdir()
        except OSError:
            pass
        raise SnapshotError(f"快照发布失败（{snapshot_id}）：{e}") from e

    return {
        "snapshot_id": snapshot_id,
        "path": str(final),
        "sha256": sha,
        "size": final.stat().st_size,
    }


__all__ = ["publish_snapshot", "SnapshotError"]
