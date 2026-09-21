# run_v1 管道详解（商用轨）

> 对应主项目 `plugins/commerce/run_v1.py`，tag `traceable-v1.0.3`。

## 阶段链（顺序执行，任一步失败即显性终止并登记）

| # | 阶段 | 做什么 | 失败形态 |
|---|---|---|---|
| 1 | raw 落地 | CSV 不可变批次入库，产出 batch_id + sha256 | LedgerError / 文件不可读 |
| 2 | 体检 | 行列数、人话问题清单（severity + 修复建议） | 严重问题按策略拦截 |
| 3 | dbt 建模 | 从不可变 raw 批次物化 dwd_base | ModelingError |
| 3.5 | 断言闸门 | 行数突变 ±40% 熔断 + 关键列唯一/非空/枚举；blocking 失败封下游 | AssertionBlocked |
| 4 | 语义编译 | 指标契约（schemas/metrics/commerce/*.yml）编译 | 编译错误 |
| 5 | 指标查询 | 经 Semantic API（唯一取数端点），产出 query_id + freshness | 取数失败 |
| 6 | 快照发布 | 临时文件 → sha256 → 原子 rename，永不覆盖 | 发布失败 |
| 7 | 双写台账 | SQLite（机查，append-only）+ JSON（人读） | provenance 缺元拒收 |

## 台账产物

- JSON：`data/runs/<run_id>.json`（run_id 形如 `r-20260826-103000-ab12`）
- SQLite：`data/runs/ledger.db`（append-only，重复 run_id 显式拒绝）

## 常见失败与排查

| 现象 | 原因 | 处置 |
|---|---|---|
| 断言闸门红灯 | 行数突变超 ±40% / 关键列空值 | 看 run 台账 assertion_report，修数据后重跑 |
| dbt 报 UnicodeDecodeError | 系统 GBK 编码 | 主项目已强制 UTF-8（PYTHONUTF8=1）；仍报错检查 Python ≥3.10 |
| 快照发布失败 | 磁盘/权限 | 检查 data/ 卷可写；快照永不覆盖，成功后必然存在 |
| provenance 拒收 | 四元组缺失 | 管道 bug 级问题，报告用户，勿手工补 |
| Windows WinError 5 | 临时目录 ACL | 换可写临时目录；与产品逻辑无关 |

## 依赖关系（架构门禁 R1-R9 摘要）

- core/ 禁止 import plugins/*；商用/研究轨禁止互相 import
- core/ 禁止硬编码 /data/* 路径（卷由 app 层注入）
- 唯一取数端点 = Semantic API；插件禁止直连指标视图
