# traceable-analysis Skill

把一份 CSV 变成「带快照、带台账、数字可溯源」的指标结论的可复用 Agent Skill。
Agent（Claude Code / Codex / Cursor / WorkBuddy 等任何支持 Agent Skills 规范的
助手）加载本 Skill 后，即可驱动 [TraceableMetrics](../..) 主项目完成可信分析，
并按 D6 数字对账铁律交付解释。

> **当前状态**：v0.1.3，仅支持商用轨（订单 CSV → 支付类指标）。
> 研究轨（A/B / RCT）规划于 v0.2。
> **版本固定**：本 Skill（`skill-v0.1.3`）仅支持主项目 `traceable-v1.0.3`；
> `traceable-v1.0.0` / `skill-v0.1.0`
> 为已知带缺陷的历史版本，**勿安装引用**。

## 工作原理

```text
Skill（本目录）
↓ 固定引用 traceable-v1.0.3
TraceableMetrics 主项目代码（clone 后 checkout 该 tag）
↓ 隔离 .venv + 锁定依赖运行
run_v1 CLI（raw→体检→断言→dbt→指标→快照→台账）
↓ 机器可读结果
台账 JSON / claims / provenance
↓ Skill 按 D6 铁律解读（代码闸门强制）
结论 + 体检 + 溯源（D7 三层边界约束交付）
```

Agent 不产生关键数字：数字一律来自管道台账并通过 `core.copilot.gate`
（D6 数字对账闸门）的代码级校验，解释正文未过闸不得交付。

## 安装

### 1. 获取主项目并固定版本

```bash
git clone https://github.com/luc38545-stack/traceable-metrics.git traceable
cd traceable
git checkout traceable-v1.0.3
python -m venv .venv
# Windows（必须用 .venv 内的 python 安装，禁止直接 pip —— 装进系统 Python 会被 check_env 拒绝）
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
# Linux / macOS
.venv/bin/python -m pip install -r requirements.lock.txt
```

### 2. 安装本 Skill 到你的 Agent

| Agent | 技能目录 | 操作 |
|---|---|---|
| Claude Code | `~/.claude/skills/` | 拷贝本目录：`cp -r skills/traceable-analysis ~/.claude/skills/` |
| WorkBuddy | `~/.workbuddy/skills/` | 拷贝本目录到该路径 |
| Codex / Cursor / 其他 | 按各家约定 | 把本目录放进其技能/规则目录，或作为上下文文档加载 |

### 3. 告诉 Agent 主项目在哪

```bash
# 建议写入 shell 配置持久化
export TRACEABLE_HOME=/path/to/traceable        # Windows: setx TRACEABLE_HOME D:\path\to\traceable
```

### 4. 自检

```bash
python ~/.claude/skills/traceable-analysis/scripts/check_env.py
# 退出码 0 + JSON "ok": true 即就绪
```

## 触发方式

对已加载本 Skill 的 Agent 说：

- 「用 traceable 分析这个 CSV：<路径>」
- 「给我一份可溯源的支付成功率分析」
- 「解读一下 run r-xxxx 的台账」（历史 run）

## 版本策略

- Skill 版本（`skill-vX.Y.Z`）与主项目版本（`traceable-vX.Y.Z`）**独立编号**。
- SKILL.md 顶部声明的 `traceable-vX.Y.Z` 是唯一支持的主项目版本；
  主项目发布新版本后，Skill 必须显式升级声明并回归 CHECKLIST.md 才能切换。
- 台账兼容性见 `references/ledger-schema.md`（当前仅 schema_version=1）。

## 已验证范围

见 `CHECKLIST.md`。跨机器验证结果会随版本更新记录在本文件底部。

| 日期 | 机器 | 结果 |
|---|---|---|
| （待跨机器验证后填写） | | |
