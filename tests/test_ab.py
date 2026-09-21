#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B 实验插件测试（架构书 §05.1 商用轨专属层 · S2 出口判据项）。

分工：test_statistics.py 管「统计本身讲不讲纪律」，这边管「实验机制可不可信」。
四个部件各有一组用例：分流（稳定/均匀/可换 salt）+ SRM + peeking + 护栏。

  A1      分流稳定：同 ID 同臂，且**跨进程**一致（专杀 Python hash() 随机种子）
  A2      分流均匀：一万 ID 的实测占比贴近配置比（专杀按 ID 前缀/字典序切分）
  A3      权重和 ≠ 1 被拒（不静默归一化）
  A4      SRM 检出：比例失衡必报，均衡必放行
  A5      SRM 拦截：失衡时抛异常，**一个结论数字都不产出**
  A6      peeking 防线：看几次就按 Bonferroni 收紧多少
  A7      护栏：BREAK / OK / 反向指标三态
  A8      GREEN 实验端到端：复用核心执行器，claims 对账 clean
  A9      RED 实验拒出 p 值（结论区零残留；SRM 警报区的 p 值属数据质量，必须留）
  A10     护栏缺数据 = NO_DATA，不静默放行
  A11     数据里出现未配置实验臂 → 被拒
  A12     换 salt = 换一套独立分流（重跑实验 / A-A 测试的地基）
  A13-A18 各类配置错误必须显式报错，不许静默降级

运行：仓库根目录下  python tests/test_ab.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH（README 已记录）
sys.path.insert(0, str(Path(__file__).resolve().parent))  # P2-01：稳定 import casekit

import casekit
from core.claims.reconcile import reconcile
from core.experiment.hashing import (
    AssignmentError,
    assign,
    assign_many,
    unit_bucket,
)
from core.statistics.executor import replay_stat
from plugins.commerce.ab_test import (
    SRMDetected,
    analyze_experiment,
    check_guardrails,
    check_srm,
    peek_guard,
    register_guardrails,
)

ARMS = {"control": 0.5, "treatment": 0.5}
SALT = "exp-2026-08-pay"
SRC = {"query_id": "Q-910", "run_id": "r-ab-test", "snapshot_id": "s-ab-test"}

#: 均匀性容差：一万 ID、50/50 下的理论波动约 ±0.7%（1σ），这里给两倍余量。
#: 用固定 ID 集 + 固定 salt，结果是确定性的，不存在偶发抖动。
UNIFORM_TOL = 0.015


def make_ids(n: int = 10000) -> list[str]:
    """混合三种 ID 形态（纯顺序 / 带杂凑尾号 / 中文），专治「按前缀或字典序切分」的偷懒实现。"""
    out = []
    for i in range(n):
        if i % 3 == 0:
            out.append(f"user_{i:05d}")
        elif i % 3 == 1:
            out.append(f"{i}-{(i * 7919) % 100003:05d}-cn")
        else:
            out.append(f"u{i:04d}")
    return out


# ---------------------------------------------------------------- A1 稳定性

def a1_stable_across_processes() -> bool:
    """同 ID 永远同臂，且跨进程一致（若用了 Python hash()，str 哈希带随机种子会串味）。"""
    probe = ["user_00001", "a-1", "张三", "u9999", "0", "z" * 20]
    local = [assign(u, SALT, ARMS) for u in probe]
    for uid, expect in zip(probe, local):
        assert assign(uid, SALT, ARMS) == expect, f"{uid} 同进程内分流不稳定"

    # 另起一个解释器（显式开随机哈希种子）算同一批 ID
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from core.experiment.hashing import assign\n"
        f"print(json.dumps([assign(u, {SALT!r}, json.loads({json.dumps(ARMS)!r})) "
        f"for u in json.loads({json.dumps(probe, ensure_ascii=False)!r})]))\n"
    )
    env = dict(os.environ, PYTHONHASHSEED="random")
    got = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, cwd=str(ROOT), env=env, timeout=60)
    assert got.returncode == 0, f"子进程失败: {got.stderr[-400:]}"
    remote = json.loads(got.stdout.strip().splitlines()[-1])
    assert remote == local, f"跨进程分流不一致：{local} vs {remote}"
    # 桶号本身也必须稳定
    assert unit_bucket("user_00001", SALT) == unit_bucket("user_00001", SALT)
    return True


# ---------------------------------------------------------------- A2 均匀性

def a2_uniform() -> bool:
    """一万 ID 分下去，实测占比贴着配置比（两臂 50/50 与三臂 2:3:5 各验一次）。"""
    ids = make_ids()
    n = len(ids)

    two = assign_many(ids, SALT, ARMS)
    for arm, exp in (("control", 0.5), ("treatment", 0.5)):
        dev = abs(len(two[arm]) / n - exp)
        assert dev < UNIFORM_TOL, f"两臂 {arm} 偏差 {dev:.2%} 超过容差 {UNIFORM_TOL:.2%}"

    three = assign_many(ids, "salt-3arm", {"a": 0.2, "b": 0.3, "c": 0.5})
    for arm, exp in (("a", 0.2), ("b", 0.3), ("c", 0.5)):
        dev = abs(len(three[arm]) / n - exp)
        assert dev < UNIFORM_TOL, f"三臂 {arm} 偏差 {dev:.2%} 超过容差 {UNIFORM_TOL:.2%}"
    # 不重不漏：每个 ID 恰好落在一个臂
    assert sum(len(v) for v in three.values()) == n, "分流后有 ID 丢失或重复"
    return True


# ---------------------------------------------------------------- A4 SRM

def a4_srm_detection() -> bool:
    """明显失衡必报；均衡必放行。"""
    bad = check_srm({"control": 5000, "treatment": 4000}, ARMS)
    assert bad["detected"] is True and bad["status"] == "SRM_DETECTED", "失衡未被检出"
    assert bad["p_value"] < 1e-4, f"失衡的 p 值应当极小，实为 {bad['p_value']}"
    assert bad["worst_arm"] == "control", "偏离最大的臂判错"
    assert "分流比例失衡" in bad["human_text"], "失衡必须给人话警报"

    ok = check_srm({"control": 1000, "treatment": 1000}, ARMS)
    assert ok["detected"] is False and ok["status"] == "OK", "均衡被误报为 SRM"
    # SRM 用 0.001 而非 0.05：误报只让你多查一次，漏报毁掉整个实验
    assert ok["alpha"] == 0.001, f"SRM 阈值应为 0.001，实为 {ok['alpha']}"
    return True


# ---------------------------------------------------------------- A6 peeking

def a6_peeking_guard() -> bool:
    """看 5 次 → alpha 从 0.05 收紧到 0.01（Bonferroni）。"""
    g = peek_guard(5, 0.05)
    assert g["method"] == "bonferroni"
    assert abs(g["alpha_adjusted"] - 0.01) < 1e-12, f"校正后的 alpha 不对: {g['alpha_adjusted']}"
    assert "5" in g["disclosure"], "披露文案必须写明查看次数"

    one = peek_guard(1, 0.05)
    assert abs(one["alpha_adjusted"] - 0.05) < 1e-12, "只看一次不该收紧"
    return True


# ---------------------------------------------------------------- A7 护栏

def a7_guardrails() -> bool:
    """护栏三态：涨破阈值 = BROKEN、小幅波动 = OK、反向指标（只许涨不许跌）同样生效。"""
    regs = register_guardrails([
        {"name": "refund_rate", "direction": "not_increase", "threshold": 0.005},
        {"name": "gmv", "direction": "not_decrease", "threshold": 0.02},
    ])
    assert all(g["registered"] for g in regs), "登记后必须标记为已登记"

    base = {"refund_rate": 0.02, "gmv": 0.90}
    # 退款率 +2pp（阈值只许 +0.5pp）→ 破；GMV -5pp（阈值只许 -2pp）→ 也破
    var = {"refund_rate": 0.04, "gmv": 0.85}
    out = check_guardrails(regs, base, var)
    by = {g["name"]: g for g in out}
    assert by["refund_rate"]["status"] == "BROKEN", "退款率破线未判出"
    assert by["gmv"]["status"] == "BROKEN", "反向指标（GMV 下跌）破线未判出"
    assert "不该上线" in by["refund_rate"]["human_text"], "破线必须给决策级人话"

    # 都在允许范围内
    out2 = check_guardrails(regs, base, {"refund_rate": 0.0205, "gmv": 0.895})
    assert all(g["status"] == "OK" for g in out2), f"允许范围内的波动被误判: {out2}"
    return True


# ---------------------------------------------------------------- A8 端到端

def a8_green_experiment_end_to_end() -> bool:
    """GREEN 实验：三色继承核心执行器，claims 对账 clean，裁决是人话。"""
    counts = (560, 1000, 500, 1000)  # (变体成功, 变体总数, 基准成功, 基准总数)
    out = analyze_experiment(
        arm_counts={"control": (500, 1000), "treatment": (560, 1000)},
        arms=ARMS, mde=0.08, period="2026-08", source=SRC, run_id="r-ab-test",
    )
    assert out["status"] == "GREEN", f"应为 GREEN，实为 {out['status']}"
    exp = out["experiment"]
    assert exp["srm"]["status"] == "OK", "均衡分流不该报 SRM"
    assert exp["significant"] is True, "+6pp 且 n=1000 应当显著"
    assert abs(out["result"]["effect"]["value"] - 0.06) < 1e-12, "效应量算错（变体必须在前）"
    assert exp["alpha_used"] == 0.05, "未偷看时不应收紧 alpha"

    r = reconcile(out["claims"], replay_stat(out, counts=counts))
    assert r["reconcile_diff"] == "clean", f"实验结论对账不通过: {r}"
    assert "上线评审" in exp["verdict"], f"裁决不是人话: {exp['verdict']}"
    return True


# ---------------------------------------------------------------- A9 RED

def a9_red_experiment_refuses_numbers() -> bool:
    """样本小到前提不满足 → 拒出结论数字。

    注意边界：SRM 的 p 值是**数据质量警报**，属于必须展示的失败信息（P4），
    不在「拒出 p 值」的范围内；被拒的是结论区的 result / claims / method。
    """
    out = analyze_experiment(
        arm_counts={"control": (4, 5), "treatment": (3, 5)},
        arms=ARMS, mde=0.08, period="2026-08", source=SRC,
    )
    assert out["status"] == "RED", f"小样本应判 RED，实为 {out['status']}"
    assert out["result"] is None, "RED 绝不能给出 result"
    assert out["claims"] == [], "RED 不出数字，自然也不该生成 claims"
    assert out["experiment"]["significant"] is False
    assert "前提不满足" in out["experiment"]["verdict"], "RED 裁决必须说清不是「没效果」"
    blob = json.dumps({"result": out["result"], "claims": out["claims"],
                       "method": out["method"]}, ensure_ascii=False, default=str)
    assert '"p_value"' not in blob, "RED 结论区不得残留 p_value 字段"
    return True


# ---------------------------------------------------------------- A10 护栏缺数

def a10_guardrail_no_data() -> bool:
    """护栏指标没数据 → NO_DATA，不许当作 OK 静默放行。"""
    regs = register_guardrails(
        [{"name": "refund_rate", "direction": "not_increase", "threshold": 0.005}])
    out = check_guardrails(regs, {"refund_rate": 0.02}, {})  # 变体侧缺数据
    assert out[0]["status"] == "NO_DATA", f"缺数据被误判为 {out[0]['status']}"
    assert "缺少数据" in out[0]["human_text"], "缺数据必须明说"
    return True


# ---------------------------------------------------------------- A12 换 salt

def a12_salt_reassigns() -> bool:
    """换 salt = 换一套独立分流：既不能纹丝不动，也不能全盘推翻。"""
    ids = make_ids(2000)
    a = assign_many(ids, "exp-v1", ARMS)
    b = assign_many(ids, "exp-v2", ARMS)
    map_a = {u: arm for arm, us in a.items() for u in us}
    map_b = {u: arm for arm, us in b.items() for u in us}
    moved = sum(1 for u in ids if map_a[u] != map_b[u])
    frac = moved / len(ids)
    # 两臂独立重分：期望约 50%。给 20%~80% 的宽区间，只卡住「没换」和「全乱换」
    assert 0.2 < frac < 0.8, f"换 salt 后重分配率 {frac:.2%}，不合常理"
    return True


# ---------------------------------------------------------------- 拒绝型用例

def a3_bad_weights():
    assign("user_1", SALT, {"control": 0.5, "treatment": 0.3})  # 权重和 0.8


def a5_srm_blocks():
    analyze_experiment(
        arm_counts={"control": (500, 1000), "treatment": (400, 800)},
        arms=ARMS, mde=0.08,
    )


def a11_unconfigured_arm():
    check_srm({"control": 1000, "treatment": 1000, "holdout": 500}, ARMS)


def a13_peek_count_zero():
    peek_guard(0, 0.05)


def a14_empty_unit_or_salt():
    assign("", SALT, ARMS)


def a14b_empty_salt():
    assign("user_1", "", ARMS)


def a15_bad_direction():
    register_guardrails([{"name": "x", "direction": "whatever", "threshold": 0.1}])


def a16_guardrail_no_name():
    register_guardrails([{"direction": "not_increase", "threshold": 0.1}])


def a17_guardrails_without_values():
    """登记了护栏却不给取值 → 必须报错（否则决策者会以为护栏查过了）。"""
    analyze_experiment(
        arm_counts={"control": (500, 1000), "treatment": (560, 1000)},
        arms=ARMS, mde=0.08,
        guardrails=[{"name": "refund_rate", "direction": "not_increase",
                     "threshold": 0.005}],
    )


def a18_srm_bad_weights():
    check_srm({"control": 1000, "treatment": 1000}, {"control": 0.5, "treatment": 0.3})


CASES = (
    ("A1 分流稳定（含跨进程一致性）", a1_stable_across_processes),
    ("A2 分流均匀（两臂 / 三臂）", a2_uniform),
    ("A4 SRM 检出：失衡必报、均衡放行", a4_srm_detection),
    ("A6 peeking 防线：Bonferroni 收紧", a6_peeking_guard),
    ("A7 护栏三态：BREAK / OK / 反向指标", a7_guardrails),
    ("A8 GREEN 实验端到端（claims 对账 clean）", a8_green_experiment_end_to_end),
    ("A9 RED 实验拒出 p 值", a9_red_experiment_refuses_numbers),
    ("A10 护栏缺数据 = NO_DATA", a10_guardrail_no_data),
    ("A12 换 salt 换一套独立分流", a12_salt_reassigns),
)

# P2-01：拒绝型用例改为声明式注册（旧写法在导入期即执行，pytest 无法逐条收集）
REJECT_CASES: tuple = (
    ("A3 权重和 ≠ 1 被拒（不静默归一化）", AssignmentError, a3_bad_weights),
    ("A5 SRM 拦截：失衡不产出任何结论", SRMDetected, a5_srm_blocks),
    ("A11 数据里出现未配置实验臂被拒", ValueError, a11_unconfigured_arm),
    ("A13 peek_count = 0 被拒", ValueError, a13_peek_count_zero),
    ("A14 unit_id 为空被拒", AssignmentError, a14_empty_unit_or_salt),
    ("A14b salt 为空被拒（无法复现分流）", AssignmentError, a14b_empty_salt),
    ("A15 护栏 direction 非法被拒", ValueError, a15_bad_direction),
    ("A16 护栏缺 name 被拒", ValueError, a16_guardrail_no_name),
    ("A17 护栏登记了却没给取值被拒", ValueError, a17_guardrails_without_values),
    ("A18 SRM 权重和 ≠ 1 被拒", ValueError, a18_srm_bad_weights),
)


if __name__ == "__main__":
    sys.exit(casekit.run_cli("TraceableMetrics A/B 实验插件测试（§05.1 商用轨 · S2 出口判据）",
                             CASES, REJECT_CASES))


test_pass, test_reject, _ = casekit.pytest_cases(CASES, REJECT_CASES)
