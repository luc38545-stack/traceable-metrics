"""诊断书生成器（架构书 §04-L3 D5 · §09.1）——全局唯一的招牌能力。

两句话概括它的地位：
- **RED 时拒出 p 值，只出诊断书。** 前提不满足还硬给个 p 值，是数据分析最常见的谎言。
- **措辞库全局唯一，两轨同源。** 论文轨只是调高完备度（追加识别假设清单与安慰剂检验模板），
  措辞本身不复制一份——否则发表材料里会出现和商业报告互相打脸的统计声明。

输出是「人话」：非程序员能读懂发生了什么、下一步该做什么（D12 零代码门槛）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.statistics import checks as _checks
from core.statistics.method_contract import RED

__all__ = ["Diagnosis", "build_diagnosis"]


# ---------------------------------------------------------------- 措辞库（唯一真源）

#: 检查项 → 人话标题
ITEM_TITLES: dict[str, str] = {
    "expected_cell_counts": "样本量太小，比例检验的近似不成立",
    "sample_size_vs_mde": "样本量不足以检出你关心的差异",
    "independence": "数据之间可能不独立",
    "missing_pattern": "存在缺失数据",
    "distribution_normality_a": "第一组数据明显偏离正态分布",
    "distribution_normality_b": "第二组数据明显偏离正态分布",
    "distribution_normality": "数据明显偏离正态分布",
}

#: 方法 → 人话推荐（按失败项给替代方案，而不是让用户自己猜）
METHOD_RECOMMENDATIONS: dict[str, dict[str, str]] = {
    "expected_cell_counts": {
        "fix": "改用 Fisher 精确检验——它不依赖大样本近似，小样本下也准。",
        "why": "比例检验用的是正态近似，四格期望频数低于 5 时近似失真，p 值不可信。",
    },
    "sample_size_vs_mde": {
        "fix": "要么补数据，要么把「想检出的差异」放大到当前样本量能看见的量级。",
        "why": "样本量决定了检验的分辨能力。样本不够时，真实差异存在也检不出来。",
    },
    "independence": {
        "fix": "确认抽样或分流方式：同一用户是否重复计入、是否按店铺/地区整群分配。",
        "why": "独立性被破坏会让 p 值偏乐观——看起来显著，其实是重复计数造成的假象。",
    },
    "missing_pattern": {
        "fix": "先弄清缺失原因再决定：能补就补，不能补就做敏感性分析，看结论是否翻转。",
        "why": "缺失若与业务结果相关（例如失败订单更可能漏记金额），结论会有系统性偏差。",
    },
    "distribution_normality": {
        "fix": "改用 Mann-Whitney 检验——它不要求数据服从正态分布。",
        "why": "t 检验假设数据近似正态；严重偏态（如金额长尾）会让结论失真。",
    },
}

#: 研究设计 → 识别假设清单（论文轨完整套件追加项）
IDENTIFICATION_ASSUMPTIONS: dict[str, list[str]] = {
    "rct": [
        "随机化是否真正执行（而非事后声称）",
        "是否存在样本损耗/不依从（attrition），损耗是否与处理相关",
        "是否发生干预溢出（spillover）——对照组是否被处理组影响",
    ],
    "observational": [
        "条件独立性：控制变量后，处理分配是否与潜在结果无关",
        "共同支撑（overlap）：处理组与对照组在协变量上是否有可比区间",
        "是否存在同时期其它冲击（同期政策/促销活动）混淆效应",
        "反向因果是否可能：是处理导致结果，还是结果预期导致处理",
    ],
    "two_group": [
        "两组除处理因素外是否可比（基线均衡）",
        "是否存在同期其它影响因素",
        "分组口径在整个观察期是否保持一致",
    ],
}

PLACEBO_TEMPLATE: list[str] = [
    "把处理开始时间人为前移——提前期不应出现效应（pretrends test）",
    "换一个理论上不该受影响的指标重跑——不应出现效应",
    "换一个理论上不该受影响的样本子集重跑——不应出现效应",
]


# ---------------------------------------------------------------- 数据结构

@dataclass
class Diagnosis:
    """一份人话诊断书（可直接进报告的 diagnosis 段）。"""

    status: str
    headline: str
    failed: list[dict[str, str]] = field(default_factory=list)
    unchecked: list[dict[str, str]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    identification_assumptions: list[str] = field(default_factory=list)
    placebo_tests: list[str] = field(default_factory=list)
    disclosure: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "headline": self.headline,
            "failed": self.failed,
            "unchecked": self.unchecked,
            "recommendations": self.recommendations,
            "identification_assumptions": self.identification_assumptions,
            "placebo_tests": self.placebo_tests,
            "disclosure": self.disclosure,
        }


def _title(name: str) -> str:
    if name in ITEM_TITLES:
        return ITEM_TITLES[name]
    # distribution_normality_a/b 兜底
    for k, v in ITEM_TITLES.items():
        if name.startswith(k):
            return v
    return name


def _rec_for(name: str) -> dict[str, str]:
    if name in METHOD_RECOMMENDATIONS:
        return METHOD_RECOMMENDATIONS[name]
    for k, v in METHOD_RECOMMENDATIONS.items():
        if name.startswith(k):
            return v
    return {"fix": "请检查该前提并重跑。", "why": "该项前提未通过。"}


# ---------------------------------------------------------------- 生成

def build_diagnosis(
    check_report: "_checks.CheckReport",
    outcome: str,
    design: str,
    method: str,
    *,
    research_mode: bool = False,
) -> Diagnosis:
    """把前提检查报告翻译成人话诊断书。

    research_mode=True（论文轨）→ 追加识别假设清单与安慰剂检验模板（§09.1 完备度提升）。
    """
    failed_names = check_report.failed_items()
    unchecked_names = check_report.unchecked_items()

    failed = []
    for it in check_report.items:
        if it.auto and not it.passed:
            rec = _rec_for(it.name)
            failed.append({
                "item": it.name,
                "title": _title(it.name),
                "detail": it.detail,
                "why": rec["why"],
                "fix": rec["fix"],
            })

    unchecked = []
    for it in check_report.items:
        if not it.auto:
            unchecked.append({
                "item": it.name,
                "title": _title(it.name),
                "detail": it.detail,
            })

    recommendations: list[str] = []
    seen: set[str] = set()
    for f in failed:
        if f["fix"] not in seen:
            seen.add(f["fix"])
            recommendations.append(f["fix"])

    if failed_names:
        status = RED
        headline = (
            f"「{method}」的前提不满足，本次不出 p 值。"
            "下面说明哪里不满足、为什么、以及该换什么方法。"
        )
        disclosure = (
            "前提不满足时给出 p 值等于给一个不可信的数字——"
            "它可能让人据此做出错误的经营决策。这是本平台拒出结论的唯一理由。"
        )
    else:
        status = "YELLOW"
        headline = (
            f"「{method}」的硬性前提已通过，但有 {len(unchecked)} 项需要你确认后才有把握下结论。"
        )
        disclosure = (
            "未检项不代表没问题，只代表机器判不了。"
            "结论旁的警告与未检项清单是强制展示项，不能关闭。"
        )

    assumptions: list[str] = []
    placebo: list[str] = []
    if research_mode:
        assumptions = list(IDENTIFICATION_ASSUMPTIONS.get(design, []))
        placebo = list(PLACEBO_TEMPLATE)

    return Diagnosis(
        status=status,
        headline=headline,
        failed=failed,
        unchecked=unchecked,
        recommendations=recommendations,
        identification_assumptions=assumptions,
        placebo_tests=placebo,
        disclosure=disclosure,
    )
