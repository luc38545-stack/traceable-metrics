"""config overlay 合并器（ADR-16 · S4 部署 diff 预览的底座）。

架构书要求（审计文档引用）：``base.yml + overlay.commerce.yml + overlay.research.yml``，
overlay 合并、**类型漂移检测**、**key 冲突检测**与**对照册生成**。

设计（争议点坐标注：架构书未规定冲突检测的精确规则，V1 取下列可测语义）：
- deep merge：overlay 覆盖 base；dict 递归，list/标量整体替换；
- 类型漂移：同一路径在 base 与 overlay 中类型不一致（如 int vs str）→ 拒绝，
  防「部署时悄悄把开关从 bool 改成字符串」这类静默语义变化；
- key 冲突：``shared.*`` 是双轨共享密钥池——任一轨 overlay 以**与 base 不同**的值
  覆盖 shared key → 冲突拒绝（两轨语义必须一致的配置被改得不一致）；
  非 shared key 允许轨差异（这正是 overlay 存在的意义）；
- 对照册：两轨 merged 配置的扁平化键值表 + 每个 key 的来源标注
  （base / overlay.commerce / overlay.research），供「部署 diff 预览」与审计留档。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

YAML = None  # 惰性 import（core 不硬性依赖 yaml；测试/调用方按需加载）

SHARED_PREFIX = "shared"
MIN_AUDIT_RETENTION_DAYS = 400


class ConfigOverlayError(ValueError):
    """overlay 合并被拒（类型漂移 / key 冲突 / 文件缺失）。"""


def validate_audit_retention_policy(base: dict[str, Any]) -> int:
    """D6：审计日志留存必须显式配置为不少于 400 个整天。"""
    shared = base.get(SHARED_PREFIX)
    days = shared.get("audit_retention_days") if isinstance(shared, dict) else None
    if type(days) is not int:  # bool is an int subclass but is not a valid day count.
        raise ConfigOverlayError(
            "D6 审计日志留存策略必须显式配置为整数，且不得少于 400 天"
        )
    if days < MIN_AUDIT_RETENTION_DAYS:
        raise ConfigOverlayError(
            f"D6 审计日志留存不得少于 {MIN_AUDIT_RETENTION_DAYS} 天：当前 {days} 天"
        )
    return days


def _load_yaml(path: Path) -> dict[str, Any]:
    global YAML
    if YAML is None:
        import yaml as _yaml

        YAML = _yaml
    if not path.exists():
        raise ConfigOverlayError(f"overlay 文件缺失: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = YAML.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigOverlayError(f"overlay 文件顶层必须是映射: {path}")
    return data


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """递归合并：overlay 覆盖 base；dict 递归，其余整体替换（不改动入参）。"""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _type_token(v: Any) -> str:
    """类型签名：dict/list/str/bool/num/None/other。

    int 与 float 同归 num（8765 vs 8765.0 语义一致，不算漂移）；
    bool 单独成类（True 与 1 语义不同——CSRF 开关不许被 1 冒充）。
    """
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, list):
        return "list"
    if isinstance(v, str):
        return "str"
    return type(v).__name__


def check_type_drift(
    base: dict[str, Any],
    overlay: dict[str, Any],
    path: str = "",
) -> list[dict[str, str]]:
    """类型漂移检测：同路径 base 与 overlay 类型签名不一致 → 登记漂移（不静默）。

    dict 递归深入；list/标量整体替换但比对类型（int/float 互通，bool 独立）；
    None 视为显式置空操作，不触发漂移。
    """
    drifts: list[dict[str, str]] = []

    def _walk(b: dict[str, Any], o: dict[str, Any], p: str) -> None:
        for k, ov in o.items():
            full = f"{p}.{k}" if p else k
            bv = b.get(k)
            if isinstance(ov, dict) and isinstance(bv, dict):
                _walk(bv, ov, full)
            elif k not in b:
                continue  # overlay 新增 key：新增配置项不算漂移
            elif _type_token(ov) != _type_token(bv):
                drifts.append({
                    "path": full,
                    "base_type": _type_token(bv) if bv is not None else "missing",
                    "overlay_type": _type_token(ov),
                })

    _walk(base, overlay, path)
    return drifts


def _iter_shared(base: dict[str, Any], prefix: str = SHARED_PREFIX) -> list[tuple[str, Any]]:
    """收集 base 中 shared.* 的扁平路径与 base 值。"""
    out: list[tuple[str, Any]] = []
    root = base.get(prefix)
    if not isinstance(root, dict):
        return out

    def _walk(d: dict[str, Any], p: str) -> None:
        for k, v in d.items():
            full = f"{p}.{k}"
            if isinstance(v, dict):
                _walk(v, full)
            else:
                out.append((full, v))

    _walk(root, prefix)
    return out


def check_shared_conflicts(
    base: dict[str, Any],
    overlay: dict[str, Any],
    track: str,
    prefix: str = SHARED_PREFIX,
) -> list[dict[str, Any]]:
    """key 冲突检测：overlay 覆盖 shared.* 且值与 base 不同 → 冲突。

    shared.* 语义：双轨必须一致的配置。任一轨改了它且改得不一致 → 冲突拒绝，
    防止「同一审计策略在两轨悄悄变成两套」这类双轨漂移。
    """
    conflicts: list[dict[str, Any]] = []
    merged = deep_merge(base, overlay)
    for full, base_val in _iter_shared(base, prefix):
        merged_val = _nested_get(merged, full)
        if merged_val != base_val:
            conflicts.append({
                "path": full,
                "track": track,
                "base_value": base_val,
                "overlay_value": merged_val,
            })
    return conflicts


def _nested_get(d: dict[str, Any], dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _nested_set(d: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def merge_track(
    base: dict[str, Any],
    overlay: dict[str, Any],
    track: str,
) -> dict[str, Any]:
    """合并单轨配置：类型漂移 + shared 冲突 → ConfigOverlayError（显性拒绝）。"""
    if track not in ("commerce", "research"):
        raise ConfigOverlayError(f"非法轨道: {track!r}")
    validate_audit_retention_policy(base)
    drifts = check_type_drift(base, overlay)
    if drifts:
        raise ConfigOverlayError(
            f"overlay 类型漂移（{track}）："
            + "; ".join(f"{d['path']}: base={d['base_type']} overlay={d['overlay_type']}"
                        for d in drifts))
    conflicts = check_shared_conflicts(base, overlay, track)
    if conflicts:
        raise ConfigOverlayError(
            f"overlay 与 base 共享密钥冲突（{track}）："
            + "; ".join(f"{c['path']} base={c['base_value']!r} overlay={c['overlay_value']!r}"
                        for c in conflicts))
    return deep_merge(base, overlay)


def build_ledger(
    base: dict[str, Any],
    commerce_overlay: dict[str, Any],
    research_overlay: dict[str, Any],
) -> dict[str, Any]:
    """对照册：两轨 merged 配置扁平化 + 每 key 来源标注（部署 diff 预览数据源）。

    来源判定：两轨 overlay 值相同 → "overlay(两轨)"; 仅某轨覆盖 → 该轨 overlay;
    未覆盖 → base。
    """
    c_merged = deep_merge(base, commerce_overlay)
    r_merged = deep_merge(base, research_overlay)
    flat_base: dict[str, Any] = {}
    _flatten(base, flat_base, "")
    flat_c: dict[str, Any] = {}
    _flatten(c_merged, flat_c, "")
    flat_r: dict[str, Any] = {}
    _flatten(r_merged, flat_r, "")

    ledger: dict[str, Any] = {}
    for key in sorted(set(flat_base) | set(flat_c) | set(flat_r)):
        cv, rv = flat_c.get(key), flat_r.get(key)
        if cv == rv:
            if key in flat_base and cv != flat_base.get(key):
                src = "overlay(两轨)"
            elif key in flat_base:
                src = "base"
            else:
                src = "overlay(两轨)"
        elif cv != flat_base.get(key) and rv == flat_base.get(key):
            src = "overlay.commerce"
        elif rv != flat_base.get(key) and cv == flat_base.get(key):
            src = "overlay.research"
        else:
            src = "两轨各自覆盖(冲突候选)"
        ledger[key] = {
            "base": flat_base.get(key),
            "commerce": cv,
            "research": rv,
            "source": src,
        }
    return {"ledger": ledger, "count": len(ledger)}


def _flatten(d: dict[str, Any], out: dict[str, Any], prefix: str) -> None:
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            _flatten(v, out, full)
        else:
            out[full] = v


def load_overlay_dir(config_dir: Path) -> dict[str, Any]:
    """从目录加载 base + 双轨 overlay，返回 {base, commerce_overlay, research_overlay}。"""
    base = _load_yaml(config_dir / "base.yml")
    validate_audit_retention_policy(base)
    return {
        "base": base,
        "commerce_overlay": _load_yaml(config_dir / "overlay.commerce.yml"),
        "research_overlay": _load_yaml(config_dir / "overlay.research.yml"),
    }


def render_diff_preview(base: dict[str, Any], overlay: dict[str, Any], track: str) -> dict:
    """部署 diff 预览：base → 该轨 merged 的扁平差异（新增/修改/删除）。"""
    merged = deep_merge(base, overlay)
    flat_b: dict[str, Any] = {}
    _flatten(base, flat_b, "")
    flat_m: dict[str, Any] = {}
    _flatten(merged, flat_m, "")
    diff: dict[str, Any] = {}
    for key in sorted(set(flat_b) | set(flat_m)):
        bv, mv = flat_b.get(key), flat_m.get(key)
        if bv != mv:
            diff[key] = {"before": bv, "after": mv}
    return {"track": track, "changed": diff, "count": len(diff)}
