"""config overlay 共享核心入口（ADR-16）。"""
from core.config.overlay import (
    ConfigOverlayError,
    MIN_AUDIT_RETENTION_DAYS,
    build_ledger,
    check_shared_conflicts,
    check_type_drift,
    deep_merge,
    load_overlay_dir,
    merge_track,
    render_diff_preview,
    validate_audit_retention_policy,
)

__all__ = [
    "ConfigOverlayError",
    "MIN_AUDIT_RETENTION_DAYS",
    "build_ledger",
    "check_shared_conflicts",
    "check_type_drift",
    "deep_merge",
    "load_overlay_dir",
    "merge_track",
    "render_diff_preview",
    "validate_audit_retention_policy",
]
