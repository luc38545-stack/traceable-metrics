# 台账 Schema（LEDGER_SCHEMA_VERSION = 1）

> 写入方：`run_v1` / `run_research`。消费方（Skill / gate CLI）只解析本版本；
> 缺失或不支持 → 显式终止（gate 退出码 4），绝不猜测。

## 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| schema_version | int | 必须 = 1；缺失 = 历史台账，闸门拒绝 |
| run_id | string | `r-YYYYMMDD-HHMMSS-xxxx` |
| track | string | `commerce` / `research`（缺失/非法拒绝） |
| started_at | string | ISO 时间 |
| batch_id | string | raw 批次指纹 |
| snapshot_id | string | `s-<run_id去前缀>` |
| snapshot_sha256 / snapshot_size | string/int | 快照完整性校验对 |
| rows | int | dwd_base 行数 |
| metric / metric_version | string/int | 指标契约名与版本 |
| metric_series | array | `[[period, value], ...]` 二元组列表 |
| query_id | string | claims 绑定的真实语义查询 id（ADR-18） |
| freshness | object | 数据新鲜度 |
| health_summary | object | 体检汇总 |
| assertion_report | object | 断言闸门报告 |
| steps | array | 各阶段 {step, ok, detail} 事件流 |
| provenance | object | 溯源四元组 + 扩展（见下） |

## provenance（任一缺失 = 不可溯源，闸门 BLOCKED）

| 字段 | 说明 |
|---|---|
| run_id | 本次 run |
| snapshot_id | 数据快照 |
| batch_id | raw 批次 |
| commit_sha | 代码指纹（build_digest 源码树 sha256） |
| dependency_lock_digest | 依赖指纹 |
| metric_contract_digest / metric_contract_version | 契约口径指纹 |
| raw_sha256 | 原始 CSV 指纹 |
| dirty | 工作树脏标记 |
| pipeline | 产线脚本路径 |

## claims 对账产物（`<run_id>.claims.json`）

由 `python -m plugins.commerce.claims_dump --run <run_id>` 生成：

| 字段 | 说明 |
|---|---|
| reconcile_diff | `clean` 才存在结论；`dirty` = 对账失败，无结论 |
| claims | 数组，每项：`{metric, period, value, unit, reconcile:"PASS", source:{query_id, run_id, snapshot_id}}` |
| failed | 对账失败明细（reconcile_diff 非 clean 时） |

D6 闸门的数字银行（NumberBank）key 格式：`<metric>#<period>`。
