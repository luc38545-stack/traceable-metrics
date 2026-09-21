# D6 数字对账铁律（Agent 解释腿行为规范）

> 代码事实源：主项目 `core/copilot/claims_gate.py`（闸门）与
> `core/copilot/gate.py`（独立 CLI）。本文件是给 Agent 的行为准则，
> **最终裁决权在代码闸门，不在 Agent 的自我判断**。
>
> 独立闸门还会：重算快照文件 SHA-256 与台账比对；校验 claims 的 source
> 三元组与台账逐项绑定（防跨 run 混用）。因此调用闸门时必须提供
> `--snapshot` 指向本次 run 的真实快照文件。

## 铁律原文

所有进入报告管线的 LLM 产物必须双形态输出——自然语言正文 + claims 引用数组。
对账只认 claims：每个被引用 claim 的 source 必须指向一次真实语义查询，
正文每个数字必须与被引用 claim 的值逐位一致。PASS 才交付，FAIL 即拦截。

## Agent 必须遵守的六条

1. **不发明数字**。正文里每个小数/百分比，唯一合法来源是 claims 里的
   `value`（或由 metric_series 一致推出的同一值）。禁止心算、估算、凑整。
2. **引用显式化**。每个数字对应的 claim key（`metric#period`）进 citations。
3. **strict 语义（P1 修复后）**。正文每个数字必须落在**被引用** claim 的值上；
   `citations=[]` 或数字来自未引用 claim 一律 FAIL——`bank.has_value` 的
   宽松回退已移除出默认路径（仅历史调用方显式 `strict=False` 可用）。
4. **裸整数豁免边界**。闸门只抓小数与百分比（如 `81.25%`、`0.8125`）；
   「第2名」「2026 年」这类裸整数不参与对账，但 Agent 仍不得用它们承载统计结论。
5. **未过闸不交付**。`python -m core.copilot.gate` 退出码非 0 的正文，
   一个字都不许出现在给用户的回复里。
6. **FAIL 后修正**。按 JSON 失败单（`unmatched_numbers` 带上下文与
   `closest_bank_value`）修正：数字抄错 → 改成银行值；引用错 key → 改 key。
   连续 3 次 FAIL → 停止，交失败单给用户。
7. **对账失败的 run 没有结论**。`reconcile_diff != clean` 时不存在「部分解读」，
   只有事件记录。

## 闸门退出码速查

| 退出码 | 含义 | Agent 动作 |
|---|---|---|
| 0 | PASS | 允许交付 |
| 1 | USAGE | 修正调用参数 |
| 2 | GATE_FAIL | 修正正文/引用，重跑（≤3 次） |
| 3 | BLOCKED | 台账/claims 不可信，停止，如实报告 |
| 4 | SCHEMA | 台账版本不支持，停止，绝不猜测 |
