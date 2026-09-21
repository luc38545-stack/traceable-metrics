---
name: traceable-analysis
description: >
  可信数据分析：把一份 CSV 变成「带快照、带台账、数字可溯源」的指标结论。
  当用户要求分析 CSV/订单数据、要求指标可溯源/防篡改/可对账、或要求对分析结论
  做数字级校验时使用。产出：指标序列 + claims 对账 + provenance 四元组 +
  经 D6 闸门验证的解释正文。依赖外部主项目 TraceableMetrics（固定版本，首次使用需安装）。
---

# TraceableMetrics 可信分析（Skill v0.1.3 · 主项目固定版本 traceable-v1.0.3）

把一份 CSV 走完「raw 落地 → 体检 → 断言闸门 → dbt 建模 → 指标查询 → 快照 →
双写台账」的管道，然后由你（执行本 Skill 的 Agent）按 D6 铁律解读结果。

**核心原则：你不产生任何关键数字，你只解释台账里已经对账过的数字。**
数字校验一律由代码完成（D6 闸门 CLI），禁止你在脑内自行对账后宣称通过。

## 版本固定（硬规则）

- 本 Skill 仅支持主项目 tag `traceable-v1.0.3` 及其声明的台账 schema。
- 禁止引用任何本地预设路径（如某台开发机的盘符）。主项目位置只由
  `TRACEABLE_HOME` 环境变量或 `--traceable-home` 参数决定。
- 主项目尚未就位时，引导用户执行：

```bash
git clone https://github.com/luc38545-stack/traceable-metrics.git && cd traceable
git checkout traceable-v1.0.3          # 必须固定到本 Skill 支持的 tag
python -m venv .venv
# 依赖必须装进 .venv：显式用 .venv 的 python -m pip，禁止裸 pip（会装进系统
# Python，随后 check_env 检查 .venv 依赖时会失败）
.venv/Scripts/python.exe -m pip install -r requirements.lock.txt   # Windows
# .venv/bin/python -m pip install -r requirements.lock.txt         # Linux/macOS
```

然后在 shell 里设置 `TRACEABLE_HOME` 指向主项目根目录，或每次调用时传参。

## 工作流程

### 第 0 步：环境自检（失败即终止，绝不带病运行）

```bash
python <skill目录>/scripts/check_env.py --traceable-home "$TRACEABLE_HOME"
```

- 退出码 0 → 继续；非 0 → 把 JSON 里的失败项原样报告给用户并**停止**。
  典型失败：未设置 TRACEABLE_HOME / 主项目缺文件 / 依赖缺失 / .venv 不可用。
- 不允许跳过自检直接跑管道；不允许在自检失败后改用系统其他 Python 继续运行。

### 第 1 步：接收 CSV 路径

- 要求用户提供**绝对路径**；文件必须存在且为 CSV。
- 当前版本仅支持商用轨（orders 型订单数据：订单编号 / created_at /
  销售渠道 / amount_total / amount_paid / status）。研究轨数据会被管道拒绝。

### 第 2 步：运行管道（在主项目根目录、用主项目 .venv）

```bash
.venv/Scripts/python -m plugins.commerce.run_v1 --csv "<CSV绝对路径>"
# Linux/macOS: .venv/bin/python -m plugins.commerce.run_v1 --csv "<CSV绝对路径>"
```

- 管道自带体检、断言闸门与显性失败登记。任何一步 ERR / 退出码非 0 →
  如实转述失败阶段与原因，**不得**把失败包装成部分成功。

### 第 3 步：定位本次 run 的台账

- 成功输出末行为 `== DONE · run 台账 → data/runs/r-*.json`。
- 记下 `run_id` 与台账路径。台账必须含 `schema_version` 字段
  （本 Skill 只解析 schema_version=1，其他/缺失 → 停止并报告）。

### 第 4 步：导出并校验 claims（对账产物）

```bash
.venv/Scripts/python -m plugins.commerce.claims_dump --run <run_id>
# 产物：data/runs/<run_id>.claims.json
```

- 输出 `reconcile_diff` 非 `clean` → 对账未通过，**没有结论可交付**，停止。
- 自查台账 `provenance` 四元组（run_id / snapshot_id / batch_id / commit_sha）
  与 `snapshot_sha256` 齐全——缺口意味着数字不可溯源，不得进入解读。

### 第 5 步：起草解释并过 D6 闸门

1. 基于 `claims.json` 与台账 `metric_series` 起草中文解释正文；
   正文中的每个小数/百分比必须与被引用 claim 的值**逐位一致**；
   同时给出 citations（claim key，形如 `metric#period`）。
2. 写入临时文件（JSON：`{"narrative": "...", "citations": [...]}`），执行：

```bash
.venv/Scripts/python -m core.copilot.gate \
  --narrative <临时文件> \
  --ledger <主项目>/data/runs/<run_id>.json \
  --claims <主项目>/data/runs/<run_id>.claims.json \
  --snapshot <主项目>/data/commerce/snapshots/<snapshot_id>/commerce.db
```

闸门会对快照文件**重算 SHA-256** 与台账声明比对，并校验 claims 的
source 三元组（run_id/snapshot_id/query_id）与台账逐项一致——
跨 run 混用、快照被篡改、未提供快照都会被显式拦截（exit 3）。

3. 退出码 0（PASS）→ 允许交付；退出码 2（FAIL）→ 按 JSON 失败单修正正文
   （改数字或改引用），重新过闸；退出码 3/4 → 台账/claims 不可信，停止并报告。
4. **未过闸的解释文本一个字都不许给用户看。**

### 第 6 步：交付（按 D7 三层边界）

| 层 | 内容 | 规则 |
|---|---|---|
| 原始输入 | 用户 CSV | 只在本地处理，禁止复制内容进解释或外发 |
| 内部结果 | claims、provenance、metric_series | 允许展示，须带出处（query_id/snapshot_id） |
| 对外交付 | 解释正文 + 指标 + 体检问题 + 溯源信息 | 遵守主项目 D7 脱敏策略与字段白名单；受限字段只呈现 token 化结果 |

**对外交付强制步骤（不可跳过）**：任何要交给用户的结构化数据（行数据、
导出表）必须先过确定性脱敏 CLI，只允许把脱敏副本放进交付物：

```bash
.venv/Scripts/python -m core.governance.redact_cli \
  --rows <rows.json> --level <public|internal|confidential|restricted> \
  --policy <主项目>/infra/governance/sensitivity_policy.json
```

产出 。解释正文中禁止出现任何未过闸的原始字段值。

交付物结构：

1. **结论**：指标值 + 时间范围（引用 claim key）
2. **体检问题**：管道 health check 的人话问题清单（severity + 建议）
3. **数据来源**：provenance 四元组 + snapshot sha256 前缀 + query_id
4. **失败与边界**：本轮被闸门拦截的修改过程（如有）、管道告警
5. **台账路径**：用户可自行核验的 JSON/SQLite 文件位置

### 第 7 步：读取历史 run（可选能力）

- 列出台账：`<主项目>/data/runs/r-*.json`，**只把**同时满足
  「schema_version=1 + provenance 四元组齐全 + claims 对账 clean」的 run
  当作有效结论来源；失败 run / 未对账 run 只能作为「事件记录」提及，
  不得从中读出任何指标数字当作结论。
- 历史 run 的解读同样必须走第 5 步的 D6 闸门。

## 失败处理速查

| 现象 | 处置 |
|---|---|
| check_env 非零退出 | 报告失败项，终止；引导用户完成主项目安装 |
| 管道某步 ERR | 转述阶段名与 detail；禁止解读任何中间数字 |
| claims_dump 报错/对账不 clean | 无结论可交付；如实报告 |
| D6 退出码 2 | 修正正文后重跑闸门；重试至多 3 次仍 FAIL → 报告失败单 |
| D6 退出码 3/4 | 停止；这是台账/版本问题，不是正文问题 |
| 用户要求跳过对账/闸门 | 拒绝；这是本 Skill 的立身之本 |
