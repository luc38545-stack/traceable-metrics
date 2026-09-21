"""复现包（S3 论文轨 · 架构书 §六「复现包」· D3 溯源 · D10 stamping）。

复现包 = 一次论文轨 run 的「完整证据袋」：原始数据、快照、预注册计划、
数据集清单、依赖锁、代码指纹，外加把这些字节串起来的 manifest（全部 sha256）。

规则：
1. **缺溯源四元组拒绝构建。** run_id/snapshot_id/batch_id/commit_sha 缺一不可
   （D3/D10：任一数字可答 snapshot × 代码指纹 × run_id）。
2. **manifest 全 sha256。** raw / snapshot / prereg / dataset 逐一哈希入账；
   verify 重算对照——任何字节改动都能被诚实报告（不伪装通过）。
3. **代码指纹独立。** code_digest.txt 记录构建时源码树 digest（build_digest）；
   verify 对照当前源码树——源码变了如实标注 code_digest 不匹配（警告级，
   不等于数据被篡改，二者分开报告）。
4. **不 import duckdb（R5）**、**不硬编码数据路径（C-4）**——一切由调用方注入。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from core.audit.ledger import build_digest, REQUIRED_PROVENANCE

__all__ = ["ReproError", "build_repro_package", "verify_repro_package"]

PACKAGE_DIR_SUFFIX = "_repro"


class ReproError(ValueError):
    """复现包构建/校验被拒。"""


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_into(pkg: Path, sub: str, src: Path) -> dict[str, Any]:
    """把 src 复制进 pkg/{sub}/，返回 {"file", "sha256", "size"}。"""
    if not Path(src).exists():
        raise ReproError(f"复现包缺少输入文件：{src}")
    dest_dir = pkg / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(src).name
    shutil.copy2(src, dest)
    return {"file": dest.relative_to(pkg).as_posix(),
            "sha256": _sha256(dest), "size": dest.stat().st_size}


def _manifest_required(manifest: dict[str, Any]) -> bool:
    need = ("package", "run_id", "track", "created_at", "provenance",
            "raw", "snapshot", "prereg", "dataset", "code_digest", "requirements")
    return all(k in manifest for k in need) and all(
        k in manifest["provenance"] for k in REQUIRED_PROVENANCE)


def build_repro_package(
    run_ledger: dict[str, Any],
    volume_root: Path,
    out_dir: Path,
    raw_path: Path,
    snapshot_db: Path,
    prereg_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    """组装一次 run 的复现包，返回 {"package_dir", "zip_path", "sha256", "entries"}。

    run_ledger 必须携带 provenance 四元组（run_id/snapshot_id/batch_id/commit_sha），
    缺失 → ReproError（D3 不可缺）。
    """
    prov = run_ledger.get("provenance") or {}
    missing = [k for k in REQUIRED_PROVENANCE if not prov.get(k)]
    if missing:
        raise ReproError(
            f"复现包拒绝构建：run 台账 provenance 缺 {missing}（run={run_ledger.get('run_id')}）"
        )
    run_id = prov["run_id"]
    if not run_id:
        raise ReproError("run_id 为空——复现包必须绑定具体 run")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pkg_dir = out_dir / f"{run_id}{PACKAGE_DIR_SUFFIX}"
    if pkg_dir.exists():
        raise ReproError(f"复现包目录已存在，拒绝覆盖：{pkg_dir}（每个 run 唯一包）")
    pkg_dir.mkdir(parents=True)

    try:
        raw_meta = _copy_into(pkg_dir, "raw", raw_path)
        snap_meta = _copy_into(pkg_dir, "snapshot", snapshot_db)
        prereg_meta = _copy_into(pkg_dir, "prereg", prereg_path)
        dataset_meta = _copy_into(pkg_dir, "dataset", manifest_path)

        # requirements.txt 与代码指纹（D3 判据的一部分）
        req_src = Path(__file__).resolve().parents[2] / "requirements.txt"
        if not req_src.exists():
            raise ReproError("仓库缺少 requirements.txt——复现包无法锁定依赖")
        req_meta = _copy_into(pkg_dir, ".", req_src)
        code_digest = build_digest()
        (pkg_dir / "code_digest.txt").write_text(code_digest + "\n", encoding="utf-8")

        manifest = {
            "package": "traceable-repro",
            "version": 1,
            "run_id": run_id,
            "track": run_ledger.get("track", ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "provenance": {k: prov[k] for k in REQUIRED_PROVENANCE},
            "raw": raw_meta,
            "snapshot": snap_meta,
            "prereg": prereg_meta,
            "dataset": dataset_meta,
            "requirements": req_meta["file"],
            "code_digest": code_digest,
        }
        (pkg_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 构建失败不留残骸
        shutil.rmtree(pkg_dir, ignore_errors=True)
        raise

    # zip + sha256（zip 内不含 zip 自身，条目即包内文件）
    zip_path = out_dir / f"{run_id}_repro.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(pkg_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(pkg_dir).as_posix())
    zip_sha = _sha256(zip_path)
    (out_dir / f"{run_id}_repro.zip.sha256").write_text(zip_sha + "\n", encoding="utf-8")

    return {
        "run_id": run_id,
        "package_dir": pkg_dir,
        "zip_path": zip_path,
        "sha256": zip_sha,
        "entries": [f.relative_to(pkg_dir).as_posix()
                    for f in sorted(pkg_dir.rglob("*")) if f.is_file()],
    }


def verify_repro_package(package_dir: Path) -> dict[str, Any]:
    """校验复现包：重算 raw/snapshot/prereg/dataset 的 sha256，对照 manifest；
    代码指纹对照当前源码树。任何不匹配都**诚实报告**（不抛异常、不伪装）。

    返回 {"ok", "raw_sha256_match", "snapshot_sha256_match", "prereg_sha256_match",
          "dataset_sha256_match", "code_digest_match", "manifest_complete",
          "warnings", "mismatches"}。
    ok = 数据字节全匹配 且 manifest 完整；code_digest 不匹配仅记 warning
    （源码在构建后改动 ≠ 数据被篡改，二者分开报告）。
    """
    pkg = Path(package_dir)
    manifest_path = pkg / "manifest.json"
    warnings: list[str] = []
    mismatches: list[str] = []

    if not manifest_path.exists():
        return {"ok": False, "manifest_complete": False, "mismatches": ["manifest.json 缺失"],
                "warnings": warnings, "raw_sha256_match": False,
                "snapshot_sha256_match": False, "prereg_sha256_match": False,
                "dataset_sha256_match": False, "code_digest_match": False}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_complete = _manifest_required(manifest)
    if not manifest_complete:
        mismatches.append("manifest 缺必需字段（run_id/provenance 四元组等）")

    def _check(entry: dict[str, Any] | None, label: str) -> bool:
        if not entry or "file" not in entry or "sha256" not in entry:
            mismatches.append(f"{label} 元数据缺失")
            return False
        f = pkg / entry["file"]
        if not f.exists():
            mismatches.append(f"{label} 文件缺失：{entry['file']}")
            return False
        cur = _sha256(f)
        if cur != entry["sha256"]:
            mismatches.append(f"{label} 字节不匹配（{entry['file']}）")
            return False
        return True

    raw_ok = _check(manifest.get("raw"), "raw")
    snap_ok = _check(manifest.get("snapshot"), "snapshot")
    prereg_ok = _check(manifest.get("prereg"), "prereg")
    dataset_ok = _check(manifest.get("dataset"), "dataset")

    code_digest_match = False
    if manifest.get("code_digest"):
        cur = build_digest()
        code_digest_match = cur == manifest["code_digest"]
        if not code_digest_match:
            warnings.append("源码树 digest 与构建时不同——代码在构建后发生过改动，"
                            "数据字节本身仍可复现（警告级，非数据篡改）")

    data_ok = raw_ok and snap_ok and prereg_ok and dataset_ok
    return {
        "ok": data_ok and manifest_complete,
        "raw_sha256_match": raw_ok,
        "snapshot_sha256_match": snap_ok,
        "prereg_sha256_match": prereg_ok,
        "dataset_sha256_match": dataset_ok,
        "code_digest_match": code_digest_match,
        "manifest_complete": manifest_complete,
        "warnings": warnings,
        "mismatches": mismatches,
    }
