#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADR-16 config overlay 测试（架构书 §六 ADR-16 · S4 部署 diff 预览底座）。

  O1 deep_merge 递归覆盖（overlay 赢；dict 递归、标量替换、不改入参）
  O2 类型漂移 → ConfigOverlayError 显性拒绝（int→str 这种静默语义变化不许发生）
  O3 shared key 冲突 → 拒绝（两轨必须一致的配置被改得不一致）
  O4 非 shared 轨差异 → 正常合并（这正是 overlay 的存在意义）
  O5 对照册：来源标注 + 扁平化键值（base / overlay.commerce / overlay.research）
  O6 示例文件加载 + 双轨 merge_track + diff 预览全链路
  O7 缺文件 → ConfigOverlayError（失败显性，不静默）

运行：仓库根目录下  pytest -q tests/test_overlay.py
"""
from pathlib import Path

import pytest

from core.config.overlay import (
    ConfigOverlayError,
    build_ledger,
    deep_merge,
    load_overlay_dir,
    merge_track,
    render_diff_preview,
)

ROOT = Path(__file__).resolve().parent.parent

BASE = {
    "shared": {"audit_retention_days": 400, "llm_provider": "omniroute"},
    "app": {"host": "127.0.0.1", "csrf_enabled": True,
            "session_timeout_minutes": 15},
    "commerce": {"port": 8765, "track": "commerce"},
    "research": {"port": 8766, "track": "research"},
}


def test_o1_deep_merge_recursive():
    """O1：dict 递归合并、标量覆盖、入参不被污染。"""
    ov = {"app": {"csrf_enabled": False}, "commerce": {"port": 9000}}
    merged = deep_merge(BASE, ov)
    assert merged["app"]["csrf_enabled"] is False, "overlay 必须覆盖标量"
    assert merged["app"]["host"] == "127.0.0.1", "未覆盖的 base 值必须保留"
    assert merged["commerce"]["port"] == 9000
    assert merged["research"]["port"] == 8766
    assert BASE["app"]["csrf_enabled"] is True, "deep_merge 不得污染入参"
    assert BASE["commerce"]["port"] == 8765


def test_o2_type_drift_rejected():
    """O2：同路径类型漂移（bool→str）→ ConfigOverlayError 显性拒绝。"""
    ov = {"app": {"csrf_enabled": "yes"}}   # bool → str 漂移
    with pytest.raises(ConfigOverlayError, match="类型漂移"):
        merge_track(BASE, ov, "commerce")


def test_o3_shared_conflict_rejected():
    """O3：overlay 覆盖 shared.* 且值不同 → 冲突拒绝（双轨一致性）。"""
    ov = {"shared": {"audit_retention_days": 30}}
    with pytest.raises(ConfigOverlayError, match="共享密钥冲突"):
        merge_track(BASE, ov, "commerce")
    # 与 base 相同的覆盖不算冲突
    ok = merge_track(BASE, {"shared": {"audit_retention_days": 400}}, "commerce")
    assert ok["shared"]["audit_retention_days"] == 400


def test_o4_track_diff_allowed():
    """O4：非 shared 轨差异 → 正常合并（overlay 的意义）。"""
    c = merge_track(BASE, {"app": {"session_timeout_minutes": 30},
                           "commerce": {"port": 8765}}, "commerce")
    r = merge_track(BASE, {"research": {"k_anonymity_default": 5}}, "research")
    assert c["app"]["session_timeout_minutes"] == 30
    assert r["app"]["session_timeout_minutes"] == 15
    assert c["shared"] == BASE["shared"], "shared 必须保持 base 值"


def test_o5_ledger_source_annotation():
    """O5：对照册来源标注（base / overlay.commerce / overlay.research）。"""
    c_ov = {"app": {"session_timeout_minutes": 30}}
    r_ov = {"research": {"k_anonymity_default": 5}}
    led = build_ledger(BASE, c_ov, r_ov)["ledger"]
    assert led["app.host"]["source"] == "base"
    assert led["app.session_timeout_minutes"]["source"] == "overlay.commerce"
    assert led["research.k_anonymity_default"]["source"] == "overlay.research"
    assert led["shared.audit_retention_days"]["source"] == "base"
    assert led["app.session_timeout_minutes"]["commerce"] == 30
    assert led["app.session_timeout_minutes"]["research"] == 15


def test_o6_example_files_full_chain():
    """O6：示例文件加载 → 双轨 merge → 对照册 → diff 预览全链路。"""
    data = load_overlay_dir(ROOT / "infra" / "config")
    c = merge_track(data["base"], data["commerce_overlay"], "commerce")
    r = merge_track(data["base"], data["research_overlay"], "research")
    assert c["commerce"]["port"] == 8765
    assert r["research"]["port"] == 8766
    assert r["research"]["k_anonymity_default"] == 5
    assert c["shared"] == data["base"]["shared"], "示例不得破坏 shared"

    led = build_ledger(data["base"], data["commerce_overlay"],
                       data["research_overlay"])["ledger"]
    assert led["research.k_anonymity_default"]["source"] == "overlay.research"

    diff = render_diff_preview(data["base"], data["research_overlay"], "research")
    assert diff["track"] == "research"
    assert diff["count"] >= 2, f"research overlay 至少改动 2 项：{diff}"
    assert "research.k_anonymity_default" in diff["changed"]
    assert diff["changed"]["research.k_anonymity_default"] == {
        "before": 2, "after": 5}


def test_o7_missing_file_rejected():
    """O7：overlay 文件缺失 → ConfigOverlayError（失败显性，不静默）。"""
    with pytest.raises(ConfigOverlayError, match="缺失"):
        load_overlay_dir(ROOT / "infra" / "config" / "nonexistent")
