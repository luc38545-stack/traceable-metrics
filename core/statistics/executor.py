"""统计执行器 · Python 核（架构书 §04-L3 · 附录 T 冻结项⑧）。

三条不可让步的规矩（D5 统计诚实底线）：
1. **RED 拒出 p 值。** 前提不满足时，本模块连算都不算——不是算出来自欺欺人地藏起来。
2. **方法不许串门。** 比例问题只能从 binary 候选族里选；指定 welch_t 直接报错
   （附录 T 回归用例：修正原附录 welch_t × diff_proportion 的搭配错误）。
3. **数字必须可对账。** 结论里的每个数字都生成 claim 并携带 source 三元组；
   对账基准来自**重算**而非任何模型输出（C-5：AI 永远不能成为事实源）。

宪法红线：共享核心，不 import plugins、不决定数据落在哪——输入一律由调用方注入。
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from typing import Any, Sequence

from core.claims.reconcile import validate_claim
from core.statistics import checks as _checks
from core.statistics.diagnosis import build_diagnosis
from core.statistics.method_contract import (
    GREEN,
    RED,
    YELLOW,
    CheckResult,
    MethodContractError,
    MethodRegistry,
    candidate_families,
    three_color,
)

ENGINE_VERSION = "core@2026.08"

__all__ = [
    "StatInputError", "two_proportion_test", "welch_t_test",
    "mann_whitney", "fisher_exact", "analyze", "replay_stat",
    "ENGINE_VERSION",
]


class StatInputError(ValueError):
    """统计输入不合法（样本为空、口径不匹配等）。"""


# ---------------------------------------------------------------- 底层检验

def two_proportion_test(
    s_a: int, n_a: int, s_b: int, n_b: int, alpha: float = 0.05
) -> dict[str, Any]:
    """两比例 z 检验（双侧），返回差值 + 95% CI + p。

    CI 用非合并标准误（检验用合并标准误）——这是标准做法：
    检验在 H0 成立的前提下用 pooled，区间估计不假设 H0。
    """
    if min(n_a, n_b) <= 0:
        raise StatInputError("样本量为 0，无法做比例检验")
    from statistics import NormalDist

    p1, p2 = s_a / n_a, s_b / n_b
    diff = p1 - p2
    p_pool = (s_a + s_b) / (n_a + n_b)
    se_pool = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    z = diff / se_pool if se_pool > 0 else 0.0
    p_value = 2 * (1 - NormalDist().cdf(abs(z)))

    se_unpooled = math.sqrt(p1 * (1 - p1) / n_a + p2 * (1 - p2) / n_b)
    z_crit = NormalDist().inv_cdf(1 - alpha / 2)
    return {
        "method": "two_proportion_test",
        "group_rates": {"a": p1, "b": p2},
        "effect": {"type": "diff_proportion", "value": diff,
                   "ci95": [diff - z_crit * se_unpooled, diff + z_crit * se_unpooled]},
        "p_value": float(p_value),
        "statistic": {"name": "z", "value": float(z)},
        "alpha": alpha,
    }


def welch_t_test(
    a: Sequence[float], b: Sequence[float], alpha: float = 0.05
) -> dict[str, Any]:
    """Welch t 检验（不假设方差齐性），返回均值差 + CI + p。"""
    from scipy import stats

    xs = [float(x) for x in a if x is not None]
    ys = [float(y) for y in b if y is not None]
    if len(xs) < 2 or len(ys) < 2:
        raise StatInputError("每组至少需要 2 个观测")
    res = stats.ttest_ind(xs, ys, equal_var=False)
    ci = res.confidence_interval(confidence_level=1 - alpha)
    diff = (sum(xs) / len(xs)) - (sum(ys) / len(ys))
    return {
        "method": "welch_t_test",
        "group_means": {"a": sum(xs) / len(xs), "b": sum(ys) / len(ys)},
        "effect": {"type": "diff_mean", "value": diff,
                   "ci95": [float(ci.low), float(ci.high)]},
        "p_value": float(res.pvalue),
        "statistic": {"name": "t", "value": float(res.statistic),
                      "df": float(res.df)},
        "alpha": alpha,
    }


def mann_whitney(
    a: Sequence[float], b: Sequence[float], alpha: float = 0.05
) -> dict[str, Any]:
    """Mann-Whitney U（不要求正态），效应量用 Hodges-Lehmann 估计。

    诚实边界：MW 基于秩，没有天然的均值差 CI。这里给的是
    Hodges-Lehmann 点估计；当样本过大导致配对计算昂贵时，明确标注为抽样近似。
    """
    from scipy import stats

    xs = [float(x) for x in a if x is not None]
    ys = [float(y) for y in b if y is not None]
    if not xs or not ys:
        raise StatInputError("样本为空")
    res = stats.mannwhitneyu(xs, ys, alternative="two-sided")

    # Hodges-Lehmann：所有配对差的中位数；n 过大时随机抽样近似（并如实标注）
    approx = False
    pairs: list[float]
    if len(xs) * len(ys) <= 4_000_000:
        pairs = [x - y for x in xs for y in ys]
    else:
        import random

        rng = random.Random(20260827)  # 固定种子 → 可复现
        pairs = [rng.choice(xs) - rng.choice(ys) for _ in range(500_000)]
        approx = True
    hl = float(_median(pairs))
    rbc = 2 * float(res.statistic) / (len(xs) * len(ys)) - 1  # rank-biserial 相关

    return {
        "method": "mann_whitney",
        "group_medians": {"a": float(_median(xs)), "b": float(_median(ys))},
        "effect": {"type": "hodges_lehmann_shift", "value": hl, "ci95": None,
                   "note": ("大样本下为抽样近似值" if approx
                            else "Hodges-Lehmann 位置漂移点估计（无解析 CI）")},
        "effect_supplement": {"type": "rank_biserial", "value": rbc},
        "p_value": float(res.pvalue),
        "statistic": {"name": "U", "value": float(res.statistic)},
        "alpha": alpha,
    }


def fisher_exact(s_a: int, n_a: int, s_b: int, n_b: int) -> dict[str, Any]:
    """Fisher 精确检验（小样本比例问题的正确选择），效应量为比值比 OR。"""
    from scipy import stats

    table = [[s_a, n_a - s_a], [s_b, n_b - s_b]]
    if min(n_a, n_b) <= 0 or min(min(r) for r in table) < 0:
        raise StatInputError("四格表不合法")
    or_ratio, p_value = stats.fisher_exact(table)
    return {
        # 返回登记册名 "fisher"——对外一律用冻结登记册的叫法
        "method": "fisher",
        "group_rates": {"a": s_a / n_a, "b": s_b / n_b},
        "effect": {"type": "odds_ratio", "value": float(or_ratio), "ci95": None,
                   "note": "比值比 OR；与比例差不是同一个量纲，不可直接当百分比读"},
        "p_value": float(p_value),
        "statistic": {"name": "odds_ratio", "value": float(or_ratio)},
        "alpha": 0.05,
    }


def poisson_test(
    s_a: int, n_a: int, s_b: int, n_b: int, alpha: float = 0.05
) -> dict[str, Any]:
    """两组 Poisson 率比检验（条件二项 C-test），效应量为率比 RR。

    counts 口径（count 结局）：``(events_a, exposure_a, events_b, exposure_b)``——
    **exposure 是暴露量**（人天 / 曝光次数 / 时长），不是「试验次数」：
    事件数可以超过暴露量（一人一天可发生多次），这与二分类的 n 不是一回事。

    方法：总事件数 N = s_a + s_b 固定时，在 H0（两组率相同）下
    ``s_a | N ~ Binomial(N, π)``，``π = n_a / (n_a + n_b)``（暴露占比）。
    RR 的 CI 由 p=s_a/N 的 Clopper-Pearson 精确区间经
    ``RR = (p/(1-p)) · (n_b/n_a)`` 变换得到——Poisson 率比的标准精确区间。

    诚实边界（D5）：**过离散（overdispersion）未检**。事件若聚集（方差 > 均值），
    条件二项检验会高估显著性，正确做法是负二项——登记册有、执行器未实现，
    调用会显式失败（不静默降级）。该未检项如实写进结论，不伪装成「已通过」。
    """
    if n_a <= 0 or n_b <= 0:
        raise StatInputError(
            "暴露量必须为正（count 结局的 n 是暴露量，不是试验次数；"
            f"实为 exposure_a={n_a}, exposure_b={n_b}）")
    if min(s_a, s_b) < 0:
        raise StatInputError("事件数不能为负")
    n_total_events = s_a + s_b
    if n_total_events <= 0:
        raise StatInputError("两组事件数合计为 0，率比无定义")
    from scipy import stats

    pi = n_a / (n_a + n_b)
    res = stats.binomtest(int(s_a), int(n_total_events), pi, alternative="two-sided")
    p_value = float(res.pvalue)

    rate_a, rate_b = s_a / n_a, s_b / n_b
    rr = rate_a / rate_b if rate_b > 0 else float("inf")

    # p 的 Clopper-Pearson 精确区间 → 变换为 RR 区间
    ci = res.proportion_ci(confidence_level=1 - alpha, method="exact")
    p_lo, p_hi = float(ci.low), float(ci.high)

    def _to_rr(p: float) -> float:
        if p >= 1.0:
            return float("inf")
        if p <= 0.0:
            return 0.0
        return (p / (1 - p)) * (n_b / n_a)

    return {
        "method": "poisson_test",
        "group_rates": {"a": rate_a, "b": rate_b},   # 单位暴露事件率
        "effect": {
            "type": "rate_ratio", "value": rr,
            "ci95": [_to_rr(p_lo), _to_rr(p_hi)],
            "note": ("率比 RR（条件二项精确 CI）；RR 与比例差不是同一量纲，"
                     "不可当百分比读"),
        },
        "p_value": p_value,
        "statistic": {"name": "events_a | N", "value": float(s_a),
                      "N": int(n_total_events),
                      "exposure_share_pi": pi},
        "alpha": alpha,
        "unchecked": ["过离散（overdispersion）"],
    }


def negative_binomial_test(
    s_a: int, n_a: int, s_b: int, n_b: int, alpha: float = 0.05
) -> dict[str, Any]:
    """两组负二项回归（对数链接），效应量为率比 RR = exp(β_group)。

    count 结局口径与 poisson 一致：``(events, exposure)``——exposure 是暴露量。

    为什么需要它：poisson 的条件二项检验假定事件独立同分布（方差=均值），
    事件**聚集**（过离散）时它会高估显著性。负二项（NB2）方差结构
    ``Var(Y) = μ + αμ²`` 把过离散直接建模进模型——这就是它相对 poisson
    的存在意义，因此**不再重复报「过离散未检」**（与 poisson 的诚实边界不同）。

    实现：单变量 GLM（对数链接，offset = log(exposure)），b 组为参照，
    β_group 是 a 组相对 b 组的对数率差；两组比较 = 一个回归，标准做法。

    诚实边界（如实标注，不伪装）：
    - 离散参数 α **固定为 1**（NB2 标准参数化），不做自由估计——
      两行数据的 α MLE 不稳；需要自由 α 的大样本场景应走负二项回归插件
      （登记册有、执行器未接入，调用显式失败，不静默降级）；
    - 两观测拟合 2 参数（df_resid=0）为恰好可辨识模型，Wald 推断基于
      MLE 渐近，仍然有效；scale 计算的除零警告为 statsmodels 内部行为，
      已在实现内显式压制。
    """
    import warnings

    import numpy as np
    import statsmodels.api as sm

    if n_a <= 0 or n_b <= 0:
        raise StatInputError(
            "暴露量必须为正（count 结局的 n 是暴露量，不是试验次数；"
            f"实为 exposure_a={n_a}, exposure_b={n_b}）")
    if min(s_a, s_b) < 0:
        raise StatInputError("事件数不能为负")
    if s_a + s_b <= 0:
        raise StatInputError("两组事件数合计为 0，率比无定义")

    X = np.array([[1.0, 1.0], [1.0, 0.0]])   # [截距, group_a]；b 组为参照
    y = np.array([float(s_a), float(s_b)])
    offset = np.log([float(n_a), float(n_b)])
    model = sm.GLM(y, X, family=sm.families.NegativeBinomial(), offset=offset)
    with warnings.catch_warnings():
        # df_resid=0 时 statsmodels 内部 scale = wresid²/0 → 无害除零警告
        warnings.simplefilter("ignore", RuntimeWarning)
        res = model.fit()

    beta = float(res.params[1])
    ci = res.conf_int()[1]
    rate_a, rate_b = s_a / n_a, s_b / n_b
    return {
        "method": "negative_binomial",
        "group_rates": {"a": rate_a, "b": rate_b},
        "effect": {
            "type": "rate_ratio", "value": float(np.exp(beta)),
            "ci95": [float(np.exp(ci[0])), float(np.exp(ci[1]))],
            "note": ("率比 RR = exp(β_group)（负二项回归，对数链接，b 组为参照；"
                     "NB2 方差结构 α=1 已吸收过离散；RR 与比例差不是同一量纲，"
                     "不可当百分比读）"),
        },
        "p_value": float(res.pvalues[1]),
        "statistic": {"name": "z", "value": float(res.tvalues[1]),
                      "df_model": int(res.df_model), "df_resid": int(res.df_resid)},
        "alpha": alpha,
    }


def _median(v: Sequence[float]) -> float:
    s = sorted(v)
    n = len(s)
    if n == 0:
        raise StatInputError("空样本")
    mid = n // 2
    return float(s[mid]) if n % 2 else float((s[mid - 1] + s[mid]) / 2)


# key 必须与 method_contract.CANDIDATE_FAMILIES 的登记册名**逐字一致**
# （登记册是冻结件⑧，改名要它点头；执行器只能对齐它，不能自造别名）。
# 因此这里登记为 "fisher"，而不是实现函数的名字 fisher_exact。
_IMPLEMENTED = {
    "two_proportion_test": two_proportion_test,
    "welch_t_test": welch_t_test,
    "mann_whitney": mann_whitney,
    "fisher": fisher_exact,
    "poisson_test": poisson_test,
    "negative_binomial": negative_binomial_test,
}


# ---------------------------------------------------------------- 主入口

def _fingerprint(obj: Any) -> str:
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def analyze(
    outcome: str,
    design: str,
    track: str,
    *,
    counts: tuple[int, int, int, int] | None = None,
    sample_a: Sequence[float] | None = None,
    sample_b: Sequence[float] | None = None,
    method: str | None = None,
    suite: str = _checks.SUITE_FAST,
    mde: float | None = None,
    alpha: float = 0.05,
    power: float = 0.8,
    question: str = "",
    period: str = "",
    source: dict[str, str] | None = None,
    run_id: str | None = None,
    research_mode: bool = False,
    registry: MethodRegistry | None = None,
) -> dict[str, Any]:
    """跑一次统计分析，产出符合附录 A 的结论对象。

    RED 时：``result`` 为 None，``method.diagnosis`` 为人话诊断书——**p 值一律不给**。
    方法不在候选族内 → MethodContractError（比例问题指定 welch_t 必被拒）。

    registry：本次分析使用的方法登记册（论文轨传**冻结**实例，保证分析期间
    方法集不可变；省略则用默认登记册，向后兼容）。
    """
    if not track:
        raise ValueError("track 必填（D10 stamping：缺字段拒绝入库）")

    families = candidate_families(outcome, design, registry=registry)
    chosen = method or families[0]
    if chosen not in families:
        raise MethodContractError(
            f"方法 {chosen} 不属于 {outcome}×{design} 的候选族 {families}。"
            "（例：比例差异问题不能用 welch_t）"
        )
    if chosen not in _IMPLEMENTED:
        raise MethodContractError(
            f"方法 {chosen} 已在登记册中，但执行器尚未实现；已实现：{sorted(_IMPLEMENTED)}"
        )

    # ---- 前提检查 → 三色判定
    report = _checks.run_checks(
        outcome, design, suite, counts=counts, baseline=None, mde=mde,
        sample_a=sample_a, sample_b=sample_b, alpha=alpha, power=power,
    )
    verdict: CheckResult = three_color(report.bool_map())

    # 机器判不了的前提项 → 未检项清单；有未检项则状态不低于 YELLOW
    unchecked = list(dict.fromkeys(list(verdict.unchecked_items) + report.unchecked_items()))
    status = verdict.status
    if unchecked and status == GREEN:
        status = YELLOW

    now = datetime.now()
    conclusion_id = f"ccl-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"

    result: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None

    if status == RED:
        # D5：前提不满足 → 拒出 p 值，只出诊断书。这里根本不调用检验函数。
        diagnosis = build_diagnosis(
            report, outcome, design, chosen, research_mode=research_mode
        ).to_dict()
    else:
        if outcome == "binary":
            if counts is None:
                raise StatInputError("二分类结果必须提供 counts")
            stat = None
            if chosen == "two_proportion_test":
                stat = two_proportion_test(*counts, alpha=alpha)
            elif chosen == "fisher":
                stat = fisher_exact(*counts)
            pooled = (counts[0] + counts[2]) / (counts[1] + counts[3])
            sens = _checks.sensitivity_note(counts[1], counts[3], pooled, alpha, power)
        elif outcome == "count":
            # counts = (events_a, exposure_a, events_b, exposure_b)：暴露量非试验次数
            if counts is None:
                raise StatInputError(
                    "count 结果必须提供 counts=(events_a, exposure_a, "
                    "events_b, exposure_b)")
            if chosen == "poisson_test":
                stat = poisson_test(*counts, alpha=alpha)
            elif chosen == "negative_binomial":
                stat = negative_binomial_test(*counts, alpha=alpha)
            else:
                raise MethodContractError(
                    f"{chosen} 的 count 实现未接入（count 已实现："
                    "poisson_test, negative_binomial）")
            # 过离散未检 → 进未检项清单（D5：不伪装成已通过）
            if (stat.get("unchecked") or []):
                unchecked = list(dict.fromkeys(unchecked + stat["unchecked"]))
                if status == GREEN:
                    status = YELLOW
            sens = {"mde_absolute": None, "disclosure":
                    "count 结局的灵敏度说明需以率比口径给出，V1 未实现。"}
        else:
            if sample_a is None or sample_b is None:
                raise StatInputError("连续型结果必须提供 sample_a / sample_b")
            if chosen == "welch_t_test":
                stat = welch_t_test(sample_a, sample_b, alpha)
            elif chosen == "mann_whitney":
                stat = mann_whitney(sample_a, sample_b, alpha)
            else:
                raise MethodContractError(f"{chosen} 的连续型实现未接入")
            sens = {"mde_absolute": None, "disclosure":
                    "连续型结果的灵敏度说明需以标准化效应量（Cohen's d）口径给出，V1.1 未实现。"}

        result = {
            "effect": stat["effect"],
            "p_value": stat["p_value"],
            "statistic": stat["statistic"],
            "sensitivity": {
                **(sens or {}),
                "disclosure_note": "检验灵敏度说明——阴性结果可信度参考",
            },
        }
        if status == YELLOW:
            # YELLOW ≠ PASS：警告与未检项清单是强制展示项
            result["warning"] = (
                "本次分析有前提项未经确认，结论旁必须展示本警告与未检项清单。"
            )
            result["sensitivity"]["required"] = True
        if unchecked:
            # D5：未检项清单 = 前提级（checks.unchecked_items）+ 方法级
            # （如 poisson 的过离散）。此前方法级未检项只参与降级判定、
            # 未随结论输出，属"降级了但没告诉用户为什么"——补上。
            result["unchecked_items"] = unchecked

    # ---- ADR-18：结论数字生成 claims（给了 source 就带上，并对三元组做校验）
    claims: list[dict[str, Any]] = []
    if source and result is not None:
        # period 必须带后缀：reconcile 用 {period: value} 索引，
        # 两个 claim 共用一个 period 会让对账互相覆盖。
        base = period or "analysis"
        eff = result["effect"]
        claims.append({
            "metric": f"effect:{chosen}",
            "period": f"{base}#effect",
            "operation": "difference",
            "value": eff["value"],
            "unit": eff["type"],
            "source": dict(source),
        })
        claims.append({
            "metric": f"p_value:{chosen}",
            "period": f"{base}#p_value",
            "operation": "point",
            "value": result["p_value"],
            "unit": "p_value",
            "source": dict(source),
        })
        for c in claims:
            validate_claim(c)      # 缺三元组 → ClaimError，拒绝入库

    inputs_repr = {
        "outcome": outcome, "design": design, "method": chosen,
        "counts": list(counts) if counts else None,
        "n_a": len(sample_a) if sample_a is not None else None,
        "n_b": len(sample_b) if sample_b is not None else None,
        "alpha": alpha, "power": power, "period": period or "analysis",
    }

    return {
        "conclusion_id": conclusion_id,
        "track": track,
        "question": question,
        "status": status,
        "outcome": outcome,
        "design": design,
        "method": {
            "registered_name": chosen,
            "candidate_families": families,
            "engine_version": ENGINE_VERSION,
            "checks": report.to_dict(),
            "diagnosis": diagnosis,
        },
        "result": result,
        "claims": claims,
        "provenance": {
            "run_id": run_id,
            "engine": ENGINE_VERSION,
            "input_fingerprint": _fingerprint(inputs_repr),
            "inputs": inputs_repr,
        },
        "created_at": now.isoformat(timespec="seconds"),
    }


def replay_stat(conclusion: dict[str, Any], *, counts=None, sample_a=None,
                sample_b=None) -> dict[str, Any]:
    """对账基准：用原始输入**重算**一遍，返回 {claim_key: value}。

    这是 ADR-18「对账只认 claims」在统计腿的实现——重放值来自重算，
    不来自任何模型输出（C-5）。
    """
    res = conclusion.get("result")
    if not res:
        return {}
    method = conclusion["method"]["registered_name"]
    alpha = conclusion["provenance"]["inputs"]["alpha"]
    if method == "two_proportion_test":
        stat = two_proportion_test(*counts, alpha=alpha)
    elif method == "fisher":
        stat = fisher_exact(*counts)
    elif method == "welch_t_test":
        stat = welch_t_test(sample_a, sample_b, alpha)
    elif method == "mann_whitney":
        stat = mann_whitney(sample_a, sample_b, alpha)
    elif method == "poisson_test":
        stat = poisson_test(*counts, alpha=alpha)
    elif method == "negative_binomial":
        stat = negative_binomial_test(*counts, alpha=alpha)
    else:
        return {}
    # 与 analyze 生成的 claim.period 对齐：reconcile 按 period 取值
    period = conclusion["provenance"]["inputs"].get("period", "analysis")
    return {
        f"{period}#effect": stat["effect"]["value"],
        f"{period}#p_value": stat["p_value"],
    }
