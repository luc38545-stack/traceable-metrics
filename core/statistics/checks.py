"""前提检查器（架构书 §04-L3 · 诊断书的前半生）。

共享实现一份，检查策略随轨（套件参数）：
- ``fast`` 商用默认：只跑能自动判定的硬前提，2 秒内出结果
- ``full`` 论文轨：追加缺失模式与分布检查的完整套件

**统计诚实底线（D5）**：机器判不了的就是判不了。
独立性、缺失机制（MCAR/MAR/MNAR）这类无法仅从观测数据判定的前提，
一律标记为 ``auto=False``，进「未检项清单」，绝不伪装成"已通过"。

宪法红线：本模块是共享核心，不 import plugins、不触碰任何数据路径——
所有输入都是调用方传入的数值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Sequence

SUITE_FAST = "fast"
SUITE_FULL = "full"

_ND = NormalDist()


def _z(q: float) -> float:
    """标准正态分位数（零依赖，商用快套件需要 2 秒内出结果）。"""
    return _ND.inv_cdf(q)


@dataclass
class CheckItem:
    """一项前提检查的结论。"""

    name: str
    passed: bool
    detail: str                                   # 人话说明（直接进诊断书/报告）
    evidence: dict[str, Any] = field(default_factory=dict)
    auto: bool = True                             # False = 机器判不了，需人工确认

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": self.evidence,
            "auto": self.auto,
        }


@dataclass
class CheckReport:
    """一次前提检查的完整报告。"""

    items: list[CheckItem]
    suite: str

    def bool_map(self) -> dict[str, bool]:
        """只把「机器能判且已判」的项交给三色判定状态机。

        人工确认项不参与 GREEN/RED 折叠——它们走未检项清单（YELLOW）。
        """
        # bool() 强转：scipy 返回 numpy 布尔，直接进 JSON 会炸
        return {i.name: bool(i.passed) for i in self.items if i.auto}

    def unchecked_items(self) -> list[str]:
        """需人工确认的前提项（D5：不伪装成通过）。"""
        return [i.name for i in self.items if not i.auto]

    def failed_items(self) -> list[str]:
        return [i.name for i in self.items if i.auto and not i.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "items": [i.to_dict() for i in self.items],
            "unchecked_items": self.unchecked_items(),
            "failed_items": self.failed_items(),
        }


# ---------------------------------------------------------------- 单项检查

def check_expected_cell_counts(
    success_a: int, n_a: int, success_b: int, n_b: int, min_count: float = 5.0
) -> CheckItem:
    """期望频数（Cochran 规则）：2×2 表每格期望值 ≥ min_count。

    比例类检验的正态近似前提。不满足 → 改用 Fisher 精确检验。
    """
    if min(n_a, n_b) <= 0:
        return CheckItem(
            name="expected_cell_counts",
            passed=False,
            detail="有一组样本量为 0，无法做比例检验。",
            evidence={"n_a": n_a, "n_b": n_b},
        )
    p_pool = (success_a + success_b) / (n_a + n_b)
    cells = [
        n_a * p_pool, n_a * (1 - p_pool),
        n_b * p_pool, n_b * (1 - p_pool),
    ]
    min_exp = min(cells)
    passed = min_exp >= min_count
    return CheckItem(
        name="expected_cell_counts",
        passed=passed,
        detail=(
            f"四格中最小的期望频数是 {min_exp:.1f}，"
            + ("达到 5 以上，比例检验的正态近似可用。"
               if passed else
               f"低于 {min_count:.0f}，正态近似不可靠，建议改用 Fisher 精确检验。")
        ),
        evidence={"min_expected": round(min_exp, 3), "threshold": min_count,
                  "pooled_rate": round(p_pool, 6)},
    )


def required_n_per_group(
    baseline: float, mde: float, alpha: float = 0.05, power: float = 0.8
) -> int:
    """两比例检验：检出 mde 所需每组样本量（正态近似，向上取整）。"""
    if mde <= 0:
        raise ValueError("mde 必须为正")
    p1, p2 = baseline, min(max(baseline + mde, 0.0), 1.0)
    p_bar = (p1 + p2) / 2
    z_a = _z(1 - alpha / 2)
    z_b = _z(power)
    num = (z_a * math.sqrt(2 * p_bar * (1 - p_bar))
           + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return math.ceil(num / (mde ** 2))


def check_sample_size_vs_mde(
    n_a: int, n_b: int, baseline: float, mde: float,
    alpha: float = 0.05, power: float = 0.8,
) -> CheckItem:
    """样本量 vs 最小可检出效应（MDE）：够不够检出你关心的那个差异。"""
    n_req = required_n_per_group(baseline, mde, alpha, power)
    actual = min(n_a, n_b)
    passed = actual >= n_req
    return CheckItem(
        name="sample_size_vs_mde",
        passed=passed,
        detail=(
            f"想可靠地检出 {mde:.1%} 的差异，每组至少需要 {n_req} 条样本；"
            f"当前较小的那组有 {actual} 条。"
            + ("样本量充足。" if passed else
               "样本量不足——即便真实差异存在，本次检验也大概率看不出来（阴性结果不可当结论用）。")
        ),
        evidence={"required_per_group": n_req, "actual_min_group": actual,
                  "mde": mde, "baseline": baseline, "power": power},
    )


def check_independence(design: str) -> CheckItem:
    """独立性核查。

    随机化设计 → 由设计保证；除此之外**无法从数据自动判定**（D5），
    标记 auto=False 进未检项清单。
    """
    if design == "rct":
        return CheckItem(
            name="independence",
            passed=True,
            detail="随机化设计，个体间独立性由随机化本身保证。",
            evidence={"design": design, "basis": "assumed_by_design"},
        )
    return CheckItem(
        name="independence",
        passed=True,
        detail=(
            "独立性无法从观测数据自动判定（需要知道抽样/分流方式）。"
            "若存在同一用户重复下单、或按店铺整群分配等情况，本次检验的 p 值会偏乐观。"
        ),
        evidence={"design": design, "basis": "cannot_auto_determine"},
        auto=False,
    )


def check_missing_pattern(
    n_total: int, n_missing: int, warn_rate: float = 0.05
) -> CheckItem:
    """缺失模式（论文轨完整套件）。

    诚实边界：MCAR / MAR / MNAR 三者**无法仅凭观测数据区分**——
    这是统计学的既定事实，任何号称能自动判定的实现都是在编。
    这里只报缺失率，缺失机制一律进未检项清单。
    """
    rate = (n_missing / n_total) if n_total else 0.0
    passed = rate <= warn_rate
    return CheckItem(
        name="missing_pattern",
        passed=passed,
        detail=(
            f"缺失 {n_missing} / {n_total} 条（{rate:.1%}）。"
            + ("在可接受范围内。" if passed else "缺失比例偏高，结果可能受缺失影响。")
            + " 缺失机制（MCAR/MAR/MNAR）无法从数据本身判定，需结合业务背景确认。"
        ),
        evidence={"n_total": n_total, "n_missing": n_missing,
                  "missing_rate": round(rate, 6), "warn_rate": warn_rate},
        auto=False,
    )


def check_distribution(
    sample: Sequence[float], alpha: float = 0.05, label: str = ""
) -> CheckItem:
    """分布检查（连续型结果的正态性前提）。

    n ≤ 5000 用 Shapiro-Wilk；更大的样本用 D'Agostino K²（Shapiro 在大样本上过于敏感）。
    label: 组标识（a/b），两组同名会互相覆盖检查项。
    """
    from scipy import stats  # 局部导入：只有连续型才需要，商用快套件不付这个成本

    name = f"distribution_normality_{label}" if label else "distribution_normality"
    xs = [float(x) for x in sample if x is not None]
    n = len(xs)
    if n < 3:
        return CheckItem(
            name=name,
            passed=False,
            detail=f"样本量仅 {n} 条，不足以判断分布形态。",
            evidence={"n": n},
        )
    stat, p = (stats.shapiro(xs) if n <= 5000 else stats.normaltest(xs))
    passed = bool(p > alpha)
    test_name = "Shapiro-Wilk" if n <= 5000 else "D'Agostino K²"
    return CheckItem(
        name=name,
        passed=passed,
        detail=(
            f"{test_name} 检验 p = {p:.4f}，"
            + ("不能拒绝正态分布，t 检验前提成立。" if passed else
               "分布明显偏离正态，建议改用 Mann-Whitney（不要求正态）。")
        ),
        evidence={"test": test_name, "statistic": round(float(stat), 6),
                  "p_value": round(float(p), 6), "n": n},
    )


# ---------------------------------------------------------------- 套件编排

def run_checks(
    outcome: str,
    design: str,
    suite: str = SUITE_FAST,
    *,
    counts: tuple[int, int, int, int] | None = None,
    baseline: float | None = None,
    mde: float | None = None,
    sample_a: Sequence[float] | None = None,
    sample_b: Sequence[float] | None = None,
    n_total: int | None = None,
    n_missing: int | None = None,
    alpha: float = 0.05,
    power: float = 0.8,
) -> CheckReport:
    """按结果类型与套件档位编排前提检查。

    counts: (success_a, n_a, success_b, n_b) —— 二分类结果
    sample_a / sample_b: 连续型结果的两组样本
    """
    if suite not in (SUITE_FAST, SUITE_FULL):
        raise ValueError(f"unknown suite: {suite}")

    items: list[CheckItem] = [check_independence(design)]

    if outcome == "binary":
        if counts is None:
            raise ValueError("二分类结果必须提供 counts=(success_a, n_a, success_b, n_b)")
        s_a, n_a, s_b, n_b = counts
        items.append(check_expected_cell_counts(s_a, n_a, s_b, n_b))
        base = baseline if baseline is not None else (s_a / n_a if n_a else 0.0)
        if mde:
            items.append(check_sample_size_vs_mde(n_a, n_b, base, mde, alpha, power))
    elif outcome == "continuous":
        if sample_a is None or sample_b is None:
            raise ValueError("连续型结果必须提供 sample_a 与 sample_b")
        if suite == SUITE_FULL:
            items.append(check_distribution(sample_a, alpha, label="a"))
            items.append(check_distribution(sample_b, alpha, label="b"))

    if suite == SUITE_FULL and n_total is not None and n_missing is not None:
        items.append(check_missing_pattern(n_total, n_missing))

    return CheckReport(items=items, suite=suite)


# ------------------------------------------------- 检验灵敏度说明（非事后功效）

def sensitivity_note(
    n_a: int, n_b: int, pooled_rate: float, alpha: float = 0.05, power: float = 0.8
) -> dict[str, Any]:
    """「检验灵敏度说明」——架构书明令用它替代「事后功效」。

    含义：以当前样本量，在 {power} 的把握下能检出的最小差异是多少。
    它是**实验设计属性**，不是对本次结果的事后打分——阴性结果的可信度参考。
    """
    if min(n_a, n_b) <= 0:
        return {"mde_absolute": None, "power": power, "alpha": alpha,
                "disclosure": "样本量为 0，无法给出检验灵敏度。"}
    z_a, z_b = _z(1 - alpha / 2), _z(power)
    p = min(max(pooled_rate, 0.0), 1.0)
    se_unit = math.sqrt(p * (1 - p) * (1 / n_a + 1 / n_b))
    mde = (z_a + z_b) * se_unit
    return {
        "mde_absolute": round(mde, 6),
        "power": power,
        "alpha": alpha,
        "disclosure": (
            f"以当前样本量（{n_a} vs {n_b}），本检验在 {power:.0%} 的把握下"
            f"能检出的最小差异是 {mde:.2%}。"
            "小于这个量级的真实差异，本次检验很可能看不出来——"
            "「没检出差异」不等于「没有差异」。"
        ),
    }
