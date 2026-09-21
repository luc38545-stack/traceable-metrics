#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布契约测试（公开仓收尾 · 阶段 A）。

锁定"可复现发布"的机器判据，任何一条失败都阻断发布：

- RC1 依赖摘要 = requirements.lock.txt 的 SHA-256（不是宽松 requirements.txt）
- RC2 缺锁文件 → 显式失败（不返回 None、不静默降级）
- RC3 dbt 模型变更 → 代码指纹必须变化（dbt/ 参与指纹）
- RC4 runtime lock 变更 → 代码指纹必须变化（锁文件参与代码指纹）
- RC5 源码读取失败 → 显式报错（不得静默跳过后仍产出"成功"指纹）
- RC6 git status 非零退出 → 如实返回 unknown(...)，不得误报 clean
- RC7 复现包必须携带 requirements.lock.txt（运行时锁随包交付）

设计约束（反脆弱）：
- 不做任何 mock 对 README/命令字符串的文案断言；安装流程由干净环境 smoke test 验证。
- 指纹函数通过可选 root 参数注入临时仓库，避免对真实源码树的写操作。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.audit import ledger
from core.repro.package import ReproError, build_repro_package

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = "requirements.lock.txt"


@pytest.fixture(autouse=True)
def _clear_digest_cache():
    """指纹函数带进程级缓存，前后各清一次，避免跨测试污染。"""
    ledger._build_digest_real_root.cache_clear()
    ledger._dependency_digest_tree.cache_clear()
    yield
    ledger._build_digest_real_root.cache_clear()
    ledger._dependency_digest_tree.cache_clear()


def _make_fake_repo(root: Path) -> Path:
    """构造一个最小可指纹仓库：core/plugins/schemas/infra/dbt 各一个文件 + 锁文件。"""
    for sub in ("core", "plugins", "schemas", "infra", "dbt"):
        d = root / sub
        d.mkdir(parents=True)
        (d / f"_{sub}_placeholder.py").write_text(f"# {sub}\n", encoding="utf-8")
    (root / "dbt" / "stg_orders.sql").write_text(
        "select 1 as order_id\n", encoding="utf-8")
    (root / RUNTIME_LOCK).write_text("duckdb==1.5.0\npyyaml==6.0\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------- RC1/RC2 依赖摘要

def test_rc1_dependency_digest_is_lock_file_sha256() -> None:
    """RC1：依赖摘要必须等于 requirements.lock.txt 的 SHA-256。"""
    lock = REPO_ROOT / RUNTIME_LOCK
    assert lock.exists(), f"仓库缺少 {RUNTIME_LOCK}——发布基线不完整"
    expected = hashlib.sha256(lock.read_bytes()).hexdigest()
    assert ledger.dependency_digest() == expected, (
        "dependency_digest() 未对齐 runtime lock——台账里的依赖锁摘要不可复现")


def test_rc2_dependency_digest_missing_lock_raises(tmp_path: Path) -> None:
    """RC2：缺锁文件必须显式失败，不得返回 None 冒充"无依赖约束"。"""
    with pytest.raises(Exception) as ei:
        ledger.dependency_digest(root=tmp_path)
    assert RUNTIME_LOCK in str(ei.value), "报错信息必须点名缺失的锁文件"


# ---------------------------------------------------------------- RC3/RC4/RC5 代码指纹

def test_rc3_code_digest_covers_dbt_models(tmp_path: Path) -> None:
    """RC3：修改 dbt 模型后代码指纹必须变化（dbt/ 参与指纹）。"""
    root = _make_fake_repo(tmp_path)
    d1 = ledger.build_digest(root=root)
    (root / "dbt" / "stg_orders.sql").write_text(
        "select 2 as order_id\n", encoding="utf-8")
    d2 = ledger.build_digest(root=root)
    assert d1 != d2, "dbt 模型改动未反映在代码指纹中——老 run 可冒充新代码版本"


def test_rc4_code_digest_covers_runtime_lock(tmp_path: Path) -> None:
    """RC4：修改 runtime lock 后代码指纹必须变化。"""
    root = _make_fake_repo(tmp_path)
    d1 = ledger.build_digest(root=root)
    (root / RUNTIME_LOCK).write_text("duckdb==1.5.1\npyyaml==6.0\n", encoding="utf-8")
    d2 = ledger.build_digest(root=root)
    assert d1 != d2, "依赖锁改动未反映在代码指纹中——换依赖不换指纹"


def test_rc5_code_digest_unreadable_source_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RC5：源码读取失败必须显式报错，不得静默跳过后仍产出"成功"指纹。"""
    root = _make_fake_repo(tmp_path)
    target = root / "core" / "_core_placeholder.py"
    real_read_bytes = Path.read_bytes

    def _boom(self: Path) -> bytes:
        if self == target:
            raise OSError(f"模拟读取失败：{self}")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    with pytest.raises(OSError):
        ledger.build_digest(root=root)


# ---------------------------------------------------------------- RC6 git 状态诚实性

def test_rc6_build_dirty_git_failure_returns_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RC6：git status 非零退出必须返回 unknown(...)，不得误报 clean。"""
    (tmp_path / ".git").mkdir()

    def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=["git", "status", "--porcelain"],
            returncode=128, stdout="", stderr="fatal: not a git repository",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = ledger.build_dirty(root=tmp_path)
    assert result.startswith("unknown"), (
        f"git 失败被误报为 {result!r}——dirty 判定必须诚实")


def test_rc6b_build_dirty_clean_repo_reports_false(tmp_path: Path) -> None:
    """RC6 反向：干净仓库（真实 git）必须报 false，unknown 只留给判不定的场景。"""
    import subprocess as sp

    (tmp_path / ".git").mkdir()
    init = sp.run(["git", "init", "-q"], cwd=str(tmp_path),
                  capture_output=True, text=True)
    if init.returncode != 0:
        pytest.skip("环境无可用 git，跳过真实 git 反向用例")
    assert ledger.build_dirty(root=tmp_path) == "false"


# ---------------------------------------------------------------- RC7 复现包带锁

def _run_ledger() -> dict[str, Any]:
    """最小合法 run 台账（含 provenance 四元组）。"""
    return {
        "run_id": "r-rc7-contract",
        "track": "research",
        "snapshot_sha256": "0" * 64,
        "metric": "conversion_rate",
        "metric_version": 1,
        "health_summary": {"rows": 1},
        "provenance": {
            "run_id": "r-rc7-contract",
            "snapshot_id": "s-rc7",
            "batch_id": "b-rc7",
            "commit_sha": "0" * 64,
        },
        "steps": ["gate"],
    }


def test_rc7_repro_package_contains_runtime_lock(tmp_path: Path) -> None:
    """RC7：复现包必须携带 requirements.lock.txt，manifest 指向它。"""
    raw = tmp_path / "cohort.csv"
    raw.write_text("participant_id,arm,converted\np01,a,1\n", encoding="utf-8")
    snapshot_db = tmp_path / "snapshot" / "research.db"
    snapshot_db.parent.mkdir(parents=True)
    snapshot_db.write_bytes(b"\x00" * 128)
    prereg = tmp_path / "prereg.json"
    prereg.write_text(json.dumps({"plan_id": "pre-rc7"}), encoding="utf-8")
    manifest = tmp_path / "dataset.json"
    manifest.write_text(json.dumps({"dataset_id": "ds-rc7"}), encoding="utf-8")

    info = build_repro_package(
        _run_ledger(), tmp_path / "volume", tmp_path / "out",
        raw, snapshot_db, prereg, manifest)
    assert f"{RUNTIME_LOCK}" in info["entries"], (
        f"复现包缺少 {RUNTIME_LOCK}：{info['entries']}")
    pkg_manifest = json.loads(
        (info["package_dir"] / "manifest.json").read_text(encoding="utf-8"))
    assert pkg_manifest["requirements"] == RUNTIME_LOCK


def test_rc7b_repro_package_missing_lock_raises(tmp_path: Path) -> None:
    """RC7 反向：仓库缺锁文件时复现包构建必须显式拒绝。"""
    import core.repro.package as pkg_mod

    raw = tmp_path / "cohort.csv"
    raw.write_text("participant_id,arm,converted\np01,a,1\n", encoding="utf-8")
    snapshot_db = tmp_path / "snapshot" / "research.db"
    snapshot_db.parent.mkdir(parents=True)
    snapshot_db.write_bytes(b"\x00" * 128)
    prereg = tmp_path / "prereg.json"
    prereg.write_text(json.dumps({"plan_id": "pre-rc7"}), encoding="utf-8")
    manifest = tmp_path / "dataset.json"
    manifest.write_text(json.dumps({"dataset_id": "ds-rc7"}), encoding="utf-8")

    lock_src = REPO_ROOT / RUNTIME_LOCK
    backup = lock_src.read_bytes()
    lock_src.unlink()
    try:
        with pytest.raises(ReproError):
            build_repro_package(
                _run_ledger(), tmp_path / "volume", tmp_path / "out",
                raw, snapshot_db, prereg, manifest)
    finally:
        lock_src.write_bytes(backup)
    # 引用模块仅为确保导入路径稳定（防止未来重命名导致 contract 静默失效）
    assert pkg_mod.__name__ == "core.repro.package"
