"""A/B 实验插件（架构书 §05.1 商用轨专属层）。

四个部件：分流（复用共享核心的受试者哈希域）+ **SRM 检验** + **peeking 防线** + **护栏指标登记**。
主分析不另起炉灶——直接调 `core.statistics.executor.analyze`，前提检查/三色判定/claims 对账全部继承。

为什么 SRM 排在最前面：**分流坏了，后面所有结论都是假的**。
它相当于实验的火灾警报——响了就必须停下来查分流机制，而不是继续读 p 值。
因此 SRM 命中时本插件拒绝产出任何结论（P4：失败显性化，禁止静默降级）。

宪法红线：本插件只接收**聚合计数**，不直连指标视图取数（R6：唯一取数端点 = Semantic API）。
"""
from __future__ import annotations

from typing import Any, Mapping

from core.experiment.hashing import assign, assign_many, expected_ratio
from core.statistics.executor import analyze

__all__ = ["check_srm", "peek_guard", "register_guardrails",
           "analyze_experiment", "assign", "assign_many"]

#: SRM 判定用更严的阈值：分流是机制问题，宁可多报一次假警报，也不能漏掉一次真故障
SRM_ALPHA = 0.001


class SRMDetected(RuntimeError):
    """样本比例失衡（Sample Ratio Mismatch）：分流机制可疑，结论不可用。"""


def check_srm(
    arm_counts: Mapping[str, int],
    arms: Mapping[str, float],
    alpha: float = SRM_ALPHA,
) -> dict[str, Any]:
    """SRM 检验：实际分流人数 vs 预期比例，卡方拟合优度。

    alpha 默认 0.001（而非 0.05）——SRM 的代价不对称：
    误报只是让你多查一次，漏报会让整个实验的结论建立在坏数据上。
    """
    from scipy import stats

    arms_in_data = list(arm_counts)
    for arm in set(arms) - set(arms_in_data):
        raise ValueError(f"实验臂 {arm} 在 arms 配置里有，但数据里没有（漏了样本还是漏了配置？）")
    for arm in set(arms_in_data) - set(arms):
        raise ValueError(f"数据里出现了未配置的实验臂 {arm}")

    # 与 assign() 同一个规矩：权重和必须是 1，不静默归一化。
    # 否则配置笔误（0.5 + 0.3）会在 assign 处报错、却在 SRM 处被悄悄当成 62.5/37.5 放行。
    total_w = sum(arms.values())
    if abs(total_w - 1.0) > 1e-9:
        raise ValueError(
            f"实验臂权重之和为 {total_w}，不是 1。请显式写全"
            "（与分流同一个规矩：不静默归一化，避免掩盖配置笔误）。"
        )

    total = sum(arm_counts.values())
    if total <= 0:
        raise ValueError("总样本量为 0")

    ratio = expected_ratio(arms)
    observed = [arm_counts[a] for a in arms_in_data]
    expected = [total * ratio[a] for a in arms_in_data]
    chi2, p = stats.chisquare(f_obs=observed, f_exp=expected)

    detected = bool(p < alpha)
    worst = max(arms_in_data,
                key=lambda a: abs(arm_counts[a] / total - ratio[a]))
    return {
        "status": "SRM_DETECTED" if detected else "OK",
        "p_value": float(p),
        "alpha": alpha,
        "chi2": float(chi2),
        "total": total,
        "observed_ratio": {a: arm_counts[a] / total for a in arms_in_data},
        "expected_ratio": ratio,
        "worst_arm": worst,
        "detected": detected,
        "human_text": (
            f"分流比例失衡：{worst} 组实际占比 {arm_counts[worst]/total:.2%}，"
            f"预期 {ratio[worst]:.2%}（卡方检验 p = {p:.2e}）。"
            "这说明分流或埋点可能有问题——先查分流机制，别急着读结论。"
            if detected else
            f"分流比例与预期一致（卡方检验 p = {p:.3f}），未发现样本比例失衡。"
        ),
    }


def peek_guard(peek_count: int, alpha: float = 0.05) -> dict[str, Any]:
    """peeking 防线：中途偷看几次，就把显著性门槛收紧多少。

    为什么需要：固定样本量的显著性检验，每多看一次就多一次"撞上假阳性"的机会。
    看 10 次能把实际假阳性率从 5% 推到 20% 以上——这是 A/B 实验最常见的自欺方式。

    默认用 Bonferroni（最保守）。它牺牲功效换安全；架构书允许上更精细的序贯方法
    （O'Brien-Fleming / alpha spending），但那属于进阶选项，不在 V1 范围。
    """
    if peek_count < 1:
        raise ValueError("peek_count 至少为 1（第一次看也算一次）")
    adjusted = alpha / peek_count
    return {
        "peek_count": peek_count,
        "alpha_original": alpha,
        "alpha_adjusted": adjusted,
        "method": "bonferroni",
        "disclosure": (
            f"本次实验已查看 {peek_count} 次。"
            if peek_count == 1 else
            f"本实验中途被查看了 {peek_count} 次，显著性门槛已按 Bonferroni 收紧到 "
            f"{adjusted:.4f}（原 {alpha}）。偷看越多次，越容易撞上假阳性——"
            "要么一次看够样本量，要么就把门槛收紧。"
        ),
    }


def register_guardrails(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """护栏指标登记：这些指标不允许因为实验而变坏。

    单项格式：{"name": 指标名, "direction": "not_increase"|"not_decrease",
               "threshold": 允许恶化的幅度}
    """
    registered = []
    for m in metrics:
        name = m.get("name")
        direction = m.get("direction")
        if not name:
            raise ValueError("护栏指标必须有 name")
        if direction not in ("not_increase", "not_decrease"):
            raise ValueError(
                f"护栏指标 {name} 的 direction 必须是 not_increase 或 not_decrease，"
                f"收到 {direction!r}"
            )
        registered.append({
            "name": name,
            "direction": direction,
            "threshold": float(m.get("threshold", 0.0)),
            "registered": True,
        })
    return registered


def check_guardrails(
    guardrails: list[dict[str, Any]],
    baseline: Mapping[str, float],
    variant: Mapping[str, float],
) -> list[dict[str, Any]]:
    """逐条核对护栏指标是否被打破。"""
    findings = []
    for g in guardrails:
        name = g["name"]
        if name not in baseline or name not in variant:
            findings.append({**g, "status": "NO_DATA",
                             "human_text": f"{name} 缺少数据，无法判断护栏是否被打破。"})
            continue
        delta = float(variant[name]) - float(baseline[name])
        thr = g["threshold"]
        if g["direction"] == "not_increase":
            broken = delta > thr
            verb = "上升"
        else:
            broken = delta < -thr
            verb = "下降"
        findings.append({
            **g,
            "baseline": float(baseline[name]),
            "variant": float(variant[name]),
            "delta": delta,
            "status": "BROKEN" if broken else "OK",
            "human_text": (
                f"{name}{verb}了 {abs(delta):.2%}，超过允许的 {thr:.2%}——护栏被打破，"
                "即便主指标好看也不该上线。" if broken else
                f"{name} 变化 {delta:+.2%}，在允许范围内。"
            ),
        })
    return findings


def analyze_experiment(
    *,
    arm_counts: Mapping[str, tuple[int, int]],
    arms: Mapping[str, float],
    baseline_arm: str = "control",
    variant_arm: str = "treatment",
    outcome: str = "binary",
    design: str = "rct",
    track: str = "commerce",
    mde: float | None = None,
    peek_count: int = 1,
    alpha: float = 0.05,
    guardrails: list[dict[str, Any]] | None = None,
    guardrail_values: tuple[Mapping[str, float], Mapping[str, float]] | None = None,
    source: dict[str, str] | None = None,
    run_id: str | None = None,
    question: str = "",
    period: str = "",
) -> dict[str, Any]:
    """A/B 实验主入口：SRM → peeking 校正 → 主分析 → 护栏，四步串行。

    arm_counts: {臂名: (成功数, 总数)}
    SRM 命中 → 抛 SRMDetected，**不产出任何结论数字**。
    """
    if baseline_arm not in arm_counts or variant_arm not in arm_counts:
        raise ValueError(f"arm_counts 必须包含 {baseline_arm} 与 {variant_arm}")

    # ---- 第 1 闸：SRM。分流坏了就别往下走了。
    srm = check_srm({a: n for a, (_, n) in arm_counts.items()}, arms)
    if srm["detected"]:
        raise SRMDetected(srm["human_text"])

    # ---- 第 2 闸：peeking 校正
    guard = peek_guard(peek_count, alpha)
    alpha_adj = guard["alpha_adjusted"]

    s_b, n_b = arm_counts[baseline_arm]
    s_v, n_v = arm_counts[variant_arm]

    # ---- 第 3 步：主分析（复用共享核心的统计执行器）
    conclusion = analyze(
        outcome, design, track,
        counts=(s_v, n_v, s_b, n_b),   # 变体在前：效应量 = 变体 - 基准
        mde=mde, alpha=alpha_adj,
        question=question, period=period,
        source=source, run_id=run_id,
    )

    # ---- 第 4 步：护栏
    # 登记了护栏却没给值（或反过来）→ 报错，不静默跳过。
    # 「护栏明明登记了却没人检查」是最危险的假安全感：决策者会以为看过护栏了。
    if bool(guardrails) != bool(guardrail_values):
        raise ValueError(
            "护栏指标与护栏取值必须成对提供"
            f"（guardrails={'已登记' if guardrails else '未提供'}，"
            f"guardrail_values={'已提供' if guardrail_values else '未提供'}）。"
            "不需要护栏就都别传；登记了就必须给数据。"
        )
    guardrail_findings: list[dict[str, Any]] = []
    if guardrails and guardrail_values:
        guardrail_findings = check_guardrails(
            register_guardrails(guardrails), guardrail_values[0], guardrail_values[1]
        )

    result = conclusion.get("result")
    significant = bool(result and result["p_value"] < alpha_adj)
    broken = [g for g in guardrail_findings if g.get("status") == "BROKEN"]

    return {
        **conclusion,
        "experiment": {
            "arms": dict(arms),
            "baseline_arm": baseline_arm,
            "variant_arm": variant_arm,
            "srm": srm,
            "peek_guard": guard,
            "alpha_used": alpha_adj,
            "significant": significant,
            "guardrails": guardrail_findings,
            "guardrails_broken": bool(broken),
            "verdict": _verdict(conclusion, significant, broken, srm),
        },
    }


def _verdict(conclusion: dict, significant: bool, broken: list, srm: dict) -> str:
    """一句话裁决（人话，给决策者看）。"""
    status = conclusion["status"]
    if status == "RED":
        return ("前提不满足，本次不给结论数字——见诊断书。"
                "（注意：这不是「实验没效果」，而是「这个数据还不足以判断」）")
    if broken:
        return (f"主指标{'显著' if significant else '不显著'}，"
                f"但有 {len(broken)} 个护栏指标被打破：不建议上线。")
    if significant:
        eff = conclusion["result"]["effect"]["value"]
        return f"主指标显著变化 {eff:+.2%}，护栏未告警：可以进入上线评审。"
    return ("主指标未检出显著差异。注意：这不等于「没有效果」——"
            "请结合检验灵敏度说明判断是否只是样本量不够。")
