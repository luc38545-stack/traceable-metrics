"""raw 落地器 —— P3 原则「原始文件永不改动」的实现。

批次目录：{volume}/raw/{entity}/dt={date}/batch={id}/
写入即不可变：落地后计算 sha256 存 manifest，后续任何清洗都是新建批次。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from core.storage.db import detect_encoding

from .context import TrackContext

_ENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def land_file(ctx: TrackContext, entity: str, src: Path) -> dict:
    """把一个外部文件原样落地为不可变批次，返回批次元数据。"""
    if not src.exists():
        raise FileNotFoundError(src)
    # 审计 #9：entity 直接拼进批次目录路径——`../` 可逃逸出本轨卷。
    # 只允许 [A-Za-z0-9_-] 起始为字母/数字；任何路径分隔符、点、穿越片段一律拒绝。
    if not entity or not _ENTITY_RE.match(entity):
        raise ValueError(f"invalid entity name: {entity!r}")
    now = datetime.now()
    batch_id = f"b{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    dest_dir = ctx.raw_dir / entity / f"dt={now:%Y-%m-%d}" / f"batch={batch_id}"
    dest_dir.mkdir(parents=True, exist_ok=False)  # exist_ok=False：同批不可重放覆盖
    dest = dest_dir / src.name
    shutil.copy2(src, dest)         # 原始字节永久保留：sha256 对应你投进来的那个文件

    # 编码规范化（架构书 §04 L1「编码探测」）：下游建模/体检统一读 UTF-8。
    # 原始文件不动，非 UTF-8 时批次内另落一份 normalized.utf8.csv 供下游读取，
    # 两种编码都在 manifest 留痕，随时可回溯到原始字节。
    enc = detect_encoding(src)
    readable = dest
    if enc not in ("utf-8", "utf-8-sig"):
        readable = dest_dir / "normalized.utf8.csv"
        readable.write_bytes(
            src.read_bytes().decode(enc, errors="replace").encode("utf-8")
        )

    manifest = {
        "batch_id": batch_id,
        "track": ctx.track,
        "project_id": ctx.project_id,
        "entity": entity,
        "file": dest.name,
        "dest": str(readable),      # 批次内下游应读的路径（dbt 建模从 raw 读）
        "encoding": enc,            # 源文件的真实编码（探测结果，留痕可复核）
        "normalized": readable is not dest,
        "sha256": _sha256(dest),    # 原始文件指纹（非转码副本）
        "landed_at": now.isoformat(timespec="seconds"),
        "immutable": True,
    }
    (dest_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
