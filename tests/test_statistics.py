#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计腿测试（架构书 §04-L3 · D5 统计诚实底线 · 附录 T 冻结项⑧）。

与 test_negative.py 的分工：那边管「平台能不能拒绝坏事」，这边管「统计本身讲不讲纪律」。
正面算得准 + 负面拒得住，缺一不算落地。

  S1-S2   数值正确性与可复现（独立复算，不复用被测代码的实现路径）
  S3-S4   RED 拒出 p 值 / 方法不许串门
  S5      YELLOW ≠ PASS：未检项清单与敏感性检验是强制项
  S6-S7   ADR-18：claim 缺三元组被拒、篡改必 FAIL
  S8      非正态被判出并给出替代方法
  S9      D10：track 必填
  S10     措辞纪律：灵敏度说明不得写成「事后功效」
  S11-S12 小样本走 Fisher、连续型两条路都能出结论

运行：仓库根目录下  python tests/test_statistics.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH（README 已记录）
sys.path.insert(0, str(Path(__file__).resolve().parent))  # P2-01：稳定 import casekit

import math

from scipy import stats as sps

import casekit
from core.claims.reconcile import ClaimError, reconcile
from core.statistics import checks as ck
from core.statistics.executor import (
    analyze,
    fisher_exact,
    mann_whitney,
    replay_stat,
    two_proportion_test,
    welch_t_test,
)
from core.statistics.method_contract import MethodContractError

SRC = {"query_id": "Q-900", "run_id": "r-stat-test", "snapshot_id": "s-stat-test"}


# ---------------------------------------------------------------- S1 数值正确性

def s1_two_proportion_value() -> bool:
    """用 scipy 独立复算（被测代码用的是 statistics.NormalDist，路径不同）。"""
    s_a, n_a, s_b, n_b = 460, 1000, 500, 1000
    got = two_proportion_test(s_a, n_a, s_b, n_b)

    p1, p2 = s_a / n_a, s_b / n_b
    diff = p1 - p2
    p_pool = (s_a + s_b) / (n_a + n_b)
    se_pool = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    z = diff / se_pool
    expect_p = 2 * sps.norm.sf(abs(z))

    assert abs(got["effect"]["value"] - diff) < 1e-12, "效应量算错"
    assert abs(got["p_value"] - expect_p) < 1e-9, f"p 值算错: {got['p_value']} vs {expect_p}"
    lo, hi = got["effect"]["ci95"]
    assert lo < diff < hi, "95% CI 必须包含点估计"
    return True


def s2_green_with_claims_reconcile() -> bool:
    """GREEN 结论带 claims，且重放对账 clean（C-5：对账基准来自重算）。"""
    counts = (460, 1000, 500, 1000)
    c = analyze("binary", "rct", "commerce", counts=counts, mde=0.08,
                period="2026-08", source=SRC, run_id="r-stat-test")
    assert c["status"] == "GREEN", f"应为 GREEN，实为 {c['status']}"
    assert c["result"] is not None, "GREEN 必须出结果"
    assert len(c["claims"]) == 2, "效应量与 p 值各一条 claim"
    r = reconcile(c["claims"], replay_stat(c, counts=counts))
    assert r["reconcile_diff"] == "clean", f"对账不通过: {r}"
    return True


# ---------------------------------------------------------------- S3-S4 纪律

def s3_red_refuses_p_value() -> bool:
    """RED：前提不满足 → result 为 None，一个 p 值都不给（D5 招牌）。"""
    c = analyze("binary", "two_group", "commerce", counts=(7, 8, 5, 8), mde=0.05)
    assert c["status"] == "RED", f"小样本应判 RED，实为 {c['status']}"
    assert c["result"] is None, "RED 绝不能给出 result（含 p 值）"
    d = c["method"]["diagnosis"]
    assert d and d["failed"], "RED 必须给人话诊断书"
    # 不只是 result=None 就完事：结论体任何角落都不许残留 p 值字段
    import json

    blob = json.dumps(c, ensure_ascii=False, default=str)
    assert '"p_value"' not in blob, "RED 结论体里不得残留 p_value 字段"
    assert c["claims"] == [], "RED 不出数字，自然也不该生成 claims"
    return True


def s4_specify_welch_on_proportion() -> None:
    """比例差异问题指定 welch_t（附录 T 回归用例的被执行动作）。"""
    analyze("binary", "rct", "commerce", counts=(460, 1000, 500, 1000),
            method="welch_t_test")


def s5_yellow_is_not_pass() -> bool:
    """YELLOW 必须三件套：未检项清单 + 敏感性必做 + 强制警告。"""
    c = analyze("binary", "two_group", "commerce", counts=(460, 1000, 500, 1000), mde=0.08)
    assert c["status"] == "YELLOW", f"观察性设计应为 YELLOW，实为 {c['status']}"
    assert c["method"]["checks"]["unchecked_items"], "YELLOW 必须列出未检项"
    assert c["result"]["sensitivity"]["required"] is True, "YELLOW 必须做敏感性检验"
    assert c["result"].get("warning"), "YELLOW 必须带强制展示的警告"
    return True


# ---------------------------------------------------------------- S6-S7 ADR-18

def s6_claim_missing_source() -> None:
    """claim 缺 source 三元组（被执行动作）。"""
    bad_src = {"run_id": "r-stat-test"}  # 缺 query_id 与 snapshot_id
    analyze("binary", "rct", "commerce", counts=(460, 1000, 500, 1000),
            mde=0.08, period="2026-08", source=bad_src)


def s7_tampered_claim_fails() -> bool:
    """篡改结论数字一个字 → 对账 FAIL（ADR-18 统计腿）。"""
    counts = (460, 1000, 500, 1000)
    c = analyze("binary", "rct", "commerce", counts=counts, mde=0.08,
                period="2026-08", source=SRC)
    tampered = [dict(x) for x in c["claims"]]
    tampered[0]["value"] = tampered[0]["value"] + 0.0001
    r = reconcile(tampered, replay_stat(c, counts=counts))
    assert r["reconcile_diff"] == "MISMATCH", "篡改后仍判 clean = 对账失效"
    assert r["quality_gate"] == "blocked", "对账失败必须拦截"
    return True


# ---------------------------------------------------------------- S8 非正态

def s8_nonnormal_detected() -> bool:
    """严重偏态数据（金额长尾）在完整套件下被判出，并推荐 Mann-Whitney。"""
    # 指数型长尾，明确非正态
    base = [1.0, 1.2, 1.1, 0.9, 1.3, 8.0, 12.0, 25.0, 40.0, 60.0, 95.0, 130.0]
    rep = ck.run_checks("continuous", "rct", ck.SUITE_FULL,
                        sample_a=base, sample_b=[x * 1.5 for x in base])
    failed = rep.failed_items()
    assert any(f.startswith("distribution_normality") for f in failed), \
        f"非正态未被判出: {rep.to_dict()}"
    from core.statistics.diagnosis import build_diagnosis

    d = build_diagnosis(rep, "continuous", "rct", "welch_t_test")
    assert any("Mann-Whitney" in r for r in d.recommendations), \
        "未给出 Mann-Whitney 替代建议"
    return True


# ---------------------------------------------------------------- S9-S10 纪律

def s9_missing_track() -> None:
    """D10 stamping：track 缺失（被执行动作）。"""
    analyze("binary", "rct", "", counts=(460, 1000, 500, 1000))


def s10_no_posthoc_power_wording() -> bool:
    """措辞纪律：灵敏度说明不得写成「事后功效」（架构书 C.2 明令更名）。"""
    c = analyze("binary", "rct", "commerce", counts=(460, 1000, 500, 1000), mde=0.08)
    note = c["result"]["sensitivity"]
    body = f"{note.get('disclosure','')} {note.get('disclosure_note','')}"
    assert "事后功效" not in body, "不得再使用「事后功效」措辞"
    assert "post-hoc power" not in body.lower(), "不得使用 post-hoc power 措辞"
    assert note.get("mde_absolute") is not None, "必须给出可检出的最小差异"
    return True


# ---------------------------------------------------------------- S11-S12 通路

def s11_fisher_for_small_sample() -> bool:
    """小样本四格表：Fisher 精确检验可用且给出比值比。"""
    r = fisher_exact(7, 8, 5, 8)
    assert r["effect"]["type"] == "odds_ratio"
    assert 0 <= r["p_value"] <= 1
    # 登记册冻结名为 "fisher"（不是 fisher_exact）——执行器必须对齐冻结件
    c = analyze("binary", "two_group", "commerce", counts=(7, 8, 5, 8),
                method="fisher")
    assert c["method"]["registered_name"] == "fisher", \
        "对外必须使用登记册的冻结方法名"
    assert r["method"] == "fisher", "底层实现返回的也应是登记册名"
    return True


def s12_continuous_two_routes() -> bool:
    """连续型：welch_t 与 mann_whitney 两条路都能给出 p 值。"""
    a = [10, 12, 9, 11, 13, 10, 12, 11]
    b = [8, 9, 7, 10, 8, 9, 7, 8]
    t = welch_t_test(a, b)
    m = mann_whitney(a, b)
    assert 0 <= t["p_value"] <= 1 and 0 <= m["p_value"] <= 1
    assert t["effect"]["type"] == "diff_mean"
    assert m["effect"]["type"] == "hodges_lehmann_shift"
    # 两组差异明显，两条路都应当显著
    assert t["p_value"] < 0.05 and m["p_value"] < 0.05
    return True


# ---------------------------------------------------------------- S13-S14 count 族

def s13_count_replay_reconcile() -> bool:
    """count 结局（poisson）：结论必须能对账（C-5 对账只认 claims 的 count 侧闭环）。

    背景：replay_stat 原只覆盖 binary/continuous 方法，poisson 结论的 claims
    重放为空 → reconcile 逐条 FAIL「replay missing」——count 结论从未真正对过账，
    属「降级了但没告诉用户为什么」的同款静默缺口（本轮修复，先 RED 后 GREEN）。
    """
    counts = (120, 1000, 100, 1000)  # (events_a, exposure_a, events_b, exposure_b)
    c = analyze("count", "two_group", "research", counts=counts, mde=0.1,
                period="2026-08", source=SRC, run_id="r-stat-test")
    assert c["status"] in ("GREEN", "YELLOW"), f"count 结论状态异常: {c['status']}"
    assert c["result"] is not None, "count 结论必须有 result"
    assert len(c["claims"]) == 2, "效应量与 p 值各一条 claim"
    r = reconcile(c["claims"], replay_stat(c, counts=counts))
    assert r["reconcile_diff"] == "clean", f"count 结论对账必须 clean: {r}"
    return True


def s14_negative_binomial_available() -> bool:
    """count 族 negative_binomial：率比 RR 与手工复算一致、可对账、不因过离散降级。

    negative_binomial 自身建模过离散（方差 = μ + αμ²），因此**不应**再以
    「过离散未检」进未检项清单（与 poisson 的诚实边界不同——模型已吸收）。
    """
    counts = (120, 1000, 100, 1000)
    c = analyze("count", "two_group", "research", counts=counts,
                method="negative_binomial", mde=0.1,
                period="2026-08", source=SRC, run_id="r-stat-test")
    assert c["method"]["registered_name"] == "negative_binomial", \
        "对外必须使用登记册的冻结方法名"
    assert c["result"] is not None
    eff = c["result"]["effect"]
    assert eff["type"] == "rate_ratio", f"效应量应为率比: {eff}"
    rate_a, rate_b = 120 / 1000, 100 / 1000
    rr_expect = rate_a / rate_b
    assert abs(eff["value"] - rr_expect) < 1e-6, \
        f"RR 与手工复算不一致: {eff['value']} vs {rr_expect}"
    assert 0 <= c["result"]["p_value"] <= 1
    unchecked = c["result"].get("unchecked_items", [])
    assert "过离散" not in str(unchecked), \
        f"负二项不应再报过离散未检（模型已吸收）: {unchecked}"
    r = reconcile(c["claims"], replay_stat(c, counts=counts))
    assert r["reconcile_diff"] == "clean", f"negative_binomial 对账必须 clean: {r}"
    return True


CASES = (
    ("S1 两比例检验数值正确（scipy 独立复算）", s1_two_proportion_value),
    ("S2 GREEN 结论 claims 对账 clean", s2_green_with_claims_reconcile),
    ("S3 RED 拒出 p 值（result=None）", s3_red_refuses_p_value),
    ("S5 YELLOW 三件套（未检项/敏感性/警告）", s5_yellow_is_not_pass),
    ("S7 claim 篡改必 FAIL", s7_tampered_claim_fails),
    ("S8 非正态被判出并推荐 Mann-Whitney", s8_nonnormal_detected),
    ("S10 措辞纪律：无「事后功效」", s10_no_posthoc_power_wording),
    ("S11 小样本走 Fisher 精确检验", s11_fisher_for_small_sample),
    ("S12 连续型两条路 welch_t / mann_whitney", s12_continuous_two_routes),
    ("S13 count 结论（poisson）claims 对账 clean", s13_count_replay_reconcile),
    ("S14 count 族 negative_binomial 可用且可对账", s14_negative_binomial_available),
)

# P2-01：拒绝型用例改为声明式注册（旧写法在导入期即执行，pytest 无法逐条收集）
REJECT_CASES: tuple = (
    ("S4 比例问题指定 welch_t 被拒", MethodContractError, s4_specify_welch_on_proportion),
    ("S6 claim 缺 source 三元组被拒", ClaimError, s6_claim_missing_source),
    ("S9 缺 track 被拒（D10）", ValueError, s9_missing_track),
)


if __name__ == "__main__":
    sys.exit(casekit.run_cli("TraceableMetrics 统计腿测试（§04-L3 · D5 统计诚实底线）",
                             CASES, REJECT_CASES))


test_pass, test_reject, _ = casekit.pytest_cases(CASES, REJECT_CASES)
