#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 解释腿测试（架构书 V1-DoD 技术侧末项 · C-5 / D6 / P0-2 盲区闭合）。

用 FakeProvider 模拟 LLM（不联网、不碰真实 API key），专门测「闸门拦不拦得住」：

  C1      未配置 Provider → 显式失败（P4：不静默降级）
  C2      结论没有 claims → 拒出解释（C-5：无对账依据，LLM 不可能是事实源）
  C3      正常路径 → 双形态产物：正文（占位符被系统注入数值）+ claims（带 source 三元组）
  C4      引用不存在的 claim key → FAIL 拦截
  C5      正文出现银行外的数字（幻觉）→ FAIL，失败单点名数字
  C6      数字抄错（与银行值不一致）→ FAIL（D6：逐位一致，不留容差）
  C7      face=R 请求 commerce 结论 → 面级约束前置过滤器拦截
  C8      未知 face → 拦截
  C9      LLM 输出不是合法 JSON → 拦截（不猜）
  C10     数字提取器：抓小数/百分比，忽略裸整数（序数/年份）
  C11     不引用任何数字的正文 → 合法放行
  C12     渲染：比例类占位符变百分比、p 值原样
  C13     explain_status：配置状态可查询
  C14     RED 结论（无 claims）→ 拒出解释

运行：仓库根目录下  python tests/test_copilot.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parent))  # P2-01：稳定 import casekit

import casekit
from core.copilot import (
    ExplainGateError,
    FaceConstraintError,
    NumberBank,
    ProviderNotConfigured,
    explain,
    explain_status,
    extract_numbers,
    gate_narrative,
    render_narrative,
)
from core.copilot.provider import ENV_BASE_URL
from core.statistics.executor import analyze

SRC = {"query_id": "Q-920", "run_id": "r-copilot-test", "snapshot_id": "s-copilot-test"}


class FakeProvider:
    """模拟 LLM：喂什么回什么（测试里直接给成品 JSON）。"""

    name = "fake-llm"

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def complete(self, prompt: str) -> str:
        return self.raw


def make_green() -> dict:
    """GREEN 统计结论（两条 claims：效应量 + p 值）。"""
    return analyze(
        "binary", "rct", "commerce", counts=(460, 1000, 500, 1000),
        mde=0.08, period="2026-08", source=SRC, run_id="r-copilot-test",
    )


EFFECT_KEY = "effect:two_proportion_test#2026-08#effect"
P_KEY = "p_value:two_proportion_test#2026-08#p_value"


def happy_raw() -> str:
    return json.dumps({
        "narrative": (
            "支付成功率出现回落，效应量为 {claim:effect:two_proportion_test#2026-08#effect}"
            "，p 值 {claim:p_value:two_proportion_test#2026-08#p_value}，建议重点排查下单转化环节。"
        ),
        "citations": [EFFECT_KEY, P_KEY],
    }, ensure_ascii=False)


# ---------------------------------------------------------------- 正面路径

def c3_happy_path() -> bool:
    """正常路径：占位符被系统注入真实值；claims 是银行里的可信 claim（带三元组）。"""
    out = explain(make_green(), FakeProvider(happy_raw()))
    assert "效应量为 -4.00%" in out["narrative"], f"占位符未渲染: {out['narrative']}"
    assert "p 值" in out["narrative"]
    assert len(out["claims"]) == 2, "citations 应解析出两条 claim"
    for c in out["claims"]:
        assert c["source"]["query_id"] == "Q-920", "claim 必须带 source 三元组"
    assert out["audit"]["gate"] == "PASS"
    assert out["audit"]["cross_review"] == "not_enabled", "未启用的能力必须显式标注"
    assert out["audit"]["human_escalation"] == "not_enabled"
    return True


def c11_no_numbers() -> bool:
    """正文不引用任何数字也合法：双形态产物照常（claims 为空数组）。"""
    raw = json.dumps({"narrative": "样本量不足，结论需谨慎解读。",
                      "citations": []}, ensure_ascii=False)
    out = explain(make_green(), FakeProvider(raw))
    assert out["claims"] == []
    assert out["audit"]["gate"] == "PASS"
    return True


def c12_render_formats() -> bool:
    """渲染格式：diff_proportion → 百分比；p_value → 原样。"""
    bank = NumberBank(make_green()["claims"])
    assert render_narrative(
        "效应量 {claim:%s}" % EFFECT_KEY, bank) == "效应量 -4.00%"
    got = render_narrative("p {claim:%s}" % P_KEY, bank)
    assert got.startswith("p 0.07"), f"p 值渲染异常: {got}"
    return True


def c10_extractor() -> bool:
    """数字提取器只抓小数/百分比，忽略裸整数（第2名、2026 年）。"""
    toks = extract_numbers("第2名在 2026 年达到 87.5%，较上月提升 6%，原始 0.875 不变")
    assert toks == ["87.5%", "6%", "0.875"], f"提取结果异常: {toks}"
    assert extract_numbers("无数字的纯文本") == []
    return True


# ---------------------------------------------------------------- 拒绝型

def c1_no_provider():
    explain(make_green())  # 未注入 provider → NullProvider → 显式失败


def c2_no_claims():
    explain({"track": "commerce", "claims": [], "status": "GREEN"})


def c4_bad_citation():
    raw = json.dumps({"narrative": "结论见报告。", "citations": ["nonsense_key"]},
                     ensure_ascii=False)
    explain(make_green(), FakeProvider(raw))


def c5_hallucinated_number():
    raw = json.dumps({
        "narrative": "支付成功率下降 12.3 个百分点。",
        "citations": [EFFECT_KEY],
    }, ensure_ascii=False)
    explain(make_green(), FakeProvider(raw))


def c6_miscopied_number():
    raw = json.dumps({
        "narrative": "效应量为 -4.50%，与预期不符。",
        "citations": [EFFECT_KEY],
    }, ensure_ascii=False)
    explain(make_green(), FakeProvider(raw))


def c7_face_mismatch():
    explain(make_green(), FakeProvider(happy_raw()), face="R")


def c8_unknown_face():
    explain(make_green(), FakeProvider(happy_raw()), face="X")


def c9_not_json():
    explain(make_green(), FakeProvider("这是一段自然语言，不是 JSON。"))


def c14_red_no_claims():
    red = analyze("binary", "rct", "commerce", counts=(3, 5, 4, 5), mde=0.08)
    assert red["status"] == "RED" and red["claims"] == []
    explain(red, FakeProvider(happy_raw()))


# ---------------------------------------------------------------- 状态查询

def c13_status_query() -> bool:
    """explain_status：环境变量为空时指向默认本地网关；显式置空则报未配置。"""
    saved = os.environ.get(ENV_BASE_URL)
    try:
        os.environ.pop(ENV_BASE_URL, None)
        st = explain_status()
        assert st["configured"] is True, "默认本地网关应视为已配置"
        assert "127.0.0.1" in st["base_url"]

        os.environ[ENV_BASE_URL] = ""
        st2 = explain_status()
        assert st2["configured"] is False, "显式置空应报未配置"
    finally:
        if saved is None:
            os.environ.pop(ENV_BASE_URL, None)
        else:
            os.environ[ENV_BASE_URL] = saved
    return True


CASES = (
    ("C3 正常路径：占位符注入 + claims 带三元组", c3_happy_path),
    ("C10 数字提取器：只抓小数/百分比", c10_extractor),
    ("C11 无数字正文合法放行", c11_no_numbers),
    ("C12 渲染格式：比例→百分比、p 值原样", c12_render_formats),
    ("C13 explain_status 配置状态可查", c13_status_query),
)

# P2-01：拒绝型用例改为声明式注册（旧写法在导入期即执行，pytest 无法逐条收集）
REJECT_CASES: tuple = (
    ("C1 未配置 Provider 显式失败", ProviderNotConfigured, c1_no_provider),
    ("C2 无 claims 拒出解释（C-5）", ExplainGateError, c2_no_claims),
    ("C7 face=R × commerce 面级拦截", FaceConstraintError, c7_face_mismatch),
    ("C8 未知 face 拦截", FaceConstraintError, c8_unknown_face),
    ("C9 LLM 输出非 JSON 拦截", ExplainGateError, c9_not_json),
    ("C14 RED 结论（无 claims）拒出解释", ExplainGateError, c14_red_no_claims),
)

# 失败单内容抽检：不仅要被拒，错误信息还得点名那个出问题的数字/键
FAILMSG_CASES: tuple = (
    # 「12.3 个百分点」无 % 号 → 提取为 12.3（银行里没有）→ 拦截，点名数字本身
    ("C5 幻觉数字被点名拦截", c5_hallucinated_number, '"number": "12.3"'),
    ("C6 抄错数字被拦截（D6 逐位一致）", c6_miscopied_number, "-4.50%"),
    ("C4 引用不存在 claim 被点名拦截", c4_bad_citation, "nonsense_key"),
)


if __name__ == "__main__":
    sys.exit(casekit.run_cli(
        "TraceableMetrics LLM 解释腿测试（V1-DoD · C-5 / D6 / P0-2 盲区）",
        CASES, REJECT_CASES, FAILMSG_CASES))


test_pass, test_reject, test_failmsg = casekit.pytest_cases(
    CASES, REJECT_CASES, FAILMSG_CASES)
