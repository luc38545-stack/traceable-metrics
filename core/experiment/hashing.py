"""受试者哈希域（架构书 §04-L3 · 共享核心）。

架构书原话：「A/B 分流 + SRM + peeking 防线为商用轨插件；随机对照登记工具为论文轨插件——
**两者共用同一个受试者哈希域实现**。」

归属判定（P8' 三问）：是否本质依赖轨道语义？否。坏掉是否污染对侧？否（它连数据都不碰）。
是否含受管数据流动？否。→ **共享核心**，两轨各拿去用，不许各写一份。

三条设计硬要求：
1. **稳定**：同一个 (unit_id, salt) 永远落到同一个桶。实验跑到一半用户换组 = 实验报废。
2. **均匀**：sha256 雪崩效应，分桶不随 ID 的字典序/数字规律产生偏斜。
3. **可复现**：不依赖 Python 的 `hash()`（它带随机种子，跨进程会变），也不用 `random`。
"""
from __future__ import annotations

import hashlib
from typing import Mapping

BUCKETS = 1000  # 分桶粒度：1000 桶足够细（0.1% 粒度），且便于按比例切分


class AssignmentError(ValueError):
    """分流配置不合法。"""


def unit_bucket(unit_id: str, salt: str, buckets: int = BUCKETS) -> int:
    """把受试者稳定地映射到一个桶号 [0, buckets)。

    salt 是实验标识：换 salt = 换一套独立的分流（重跑实验、做 A/A 测试时用）。
    """
    if not unit_id:
        raise AssignmentError("unit_id 不能为空")
    if not salt:
        raise AssignmentError("salt 不能为空（没有 salt 的实验无法复现分流）")
    if buckets < 2:
        raise AssignmentError("buckets 至少为 2")
    digest = hashlib.sha256(f"{salt}:{unit_id}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % buckets


def assign(unit_id: str, salt: str, arms: Mapping[str, float]) -> str:
    """按权重把受试者分到某个实验臂。

    arms: {"control": 0.5, "treatment": 0.5}；权重不必归一化，按比例处理。
    权重和不为 1 → 报错，不静默归一化（静默归一化会掩盖配置错误）。
    """
    if not arms:
        raise AssignmentError("arms 不能为空")
    if any(w <= 0 for w in arms.values()):
        raise AssignmentError(f"权重必须为正：{dict(arms)}")
    total_w = sum(arms.values())
    if abs(total_w - 1.0) > 1e-9:
        raise AssignmentError(
            f"权重之和为 {total_w}，不是 1。请显式写全（不静默归一化，"
            "避免掩盖配置错误——多臂实验里漏写一个臂是常见事故）。"
        )

    b = unit_bucket(unit_id, salt)
    acc = 0.0
    # 排序保证权重相同臂名时的分配可复现（dict 顺序在跨进程下也应稳定，这里再兜一道）
    for arm in sorted(arms):
        acc += arms[arm]
        if b < int(acc * BUCKETS):
            return arm
    return sorted(arms)[-1]  # 浮点边界兜底：落到最后一个臂


def assign_many(
    unit_ids: list[str], salt: str, arms: Mapping[str, float]
) -> dict[str, list[str]]:
    """批量分流，返回 {臂名: [unit_id, ...]}（便于直接算各臂样本量、做 SRM）。"""
    out: dict[str, list[str]] = {arm: [] for arm in arms}
    for uid in unit_ids:
        out[assign(uid, salt, arms)].append(uid)
    return out


def expected_ratio(arms: Mapping[str, float]) -> dict[str, float]:
    """归一化后的预期分流比（SRM 检验的基准）。"""
    total = sum(arms.values())
    return {arm: w / total for arm, w in arms.items()}


__all__ = ["AssignmentError", "assign", "assign_many", "expected_ratio",
           "unit_bucket", "BUCKETS"]
