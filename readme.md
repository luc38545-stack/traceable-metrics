# TraceableMetrics · 数据分析自动化平台（V1 施工仓）

> 架构依据：《架构设计书 V5》（REV C.2 冻结版）· 宪法 C-1~C-5 生效中
> 本仓库状态：V1 端到端切片已跑通 · 快照/台账/门禁均为真实产物

## 一句话

把一份 CSV 变成「带快照、带台账、数字可追溯到 raw 文件」的指标——这就是 V1。

## 目录结构与依赖方向（冻结）

```
traceable/
├── core/            # 共享核心：永远不知道数据住在哪（C-4：禁硬编码 /data/*，CI R3 强制）
│   ├── storage/     #   存储抽象接口（C-3：core 内唯一允许 import duckdb 的地方，CI R5 强制）
│   ├── ingestion/   #   TrackContext 注入器 + raw 不可变批次落地
│   ├── modeling/    #   dbt 声明建模运行器（架构书 §04-L3，从 raw 批次物化 dwd_base）
│   ├── quality/     #   体检引擎（人话问题清单）
│   ├── semantic/    #   指标契约编译（含嵌套指标引用）+ Semantic API（唯一取数端点，含查询日志）
│   ├── claims/      #   Claim Schema 校验 + 对账（ADR-18，篡改必 FAIL）
│   ├── audit/       #   SQLite 运行台账（provenance 四元组缺一拒收：run_id/snapshot_id/batch_id/commit_sha，append-only）
│   ├── auth/        #   Capability Matrix / tool registry 授权器（级别白名单 + per-key 白名单，越权必拒+审计）
│   ├── statistics/  #   统计方法契约（候选族登记册 + 三色判定状态机 + Python 核执行器）
│   ├── experiment/  #   受试者哈希域（A/B 分流与 RCT 登记**共用一份**实现，架构书 §04-L3）
│   └── copilot/     #   LLM 解释腿（Provider 可换层 + D6 数字对账闸门 + 路由表，C-5 铁律）
├── plugins/
│   ├── commerce/    # 商用轨插件（run_v1.py = 端到端入口；webapp.py = 本地界面 8765；ab_test.py = A/B 实验）
│   └── research/    # 论文轨插件（run_research.py = 端到端管道；webapp_r.py = face=R 独立入口 8766；
│                    #   dataset_gate / prereg_hook / assertions_hook / inputs / quarto）
├── schemas/metrics/commerce/*.yml   # Metric Contract（冻结 schema；pay_success_rate 为嵌套指标 v2）
├── dbt/             # dbt 工程（dbt_project.yml + profiles.yml + models/dwd_base.sql）
├── infra/compose.yml               # ADR-13 双栈：本轨卷单挂 + 网络隔离（CI R4 静态扫描）
├── tests/           # check_dependencies.py（架构门禁 R1-R9）+ check_runtime.py（运行门禁 RT1-RT6）
│   #                + test_negative / test_statistics / test_ab / test_copilot / test_p0_credibility
│   #                + test_p1_security / test_p204_metric_boundary / test_e2e_export / test_d12_browser
│   #                + test_audit_retention（D6 留存策略 ≥400 天显性门禁）
│   #                + docker_isolation_acceptance.py（ADR-13 opt-in 真实 Docker 验收 DKR1-DKR6）
└── data/
    ├── runs/        # 台账：ledger.db（SQLite）+ r-*.json（人读）
    ├── commerce/    # 商用轨卷（只有本轨容器/进程挂载）
    └── research/    # 论文轨卷（face=R 独立卷：uploads/runs/snapshots/repro/exports，同布局）
```

依赖规则（违反 = 构建失败，见 `tests/check_dependencies.py`）：
- `core/` ✗→ `plugins/*`（R1）· `plugins/commerce` ✗→ `plugins/research`（R2）
- `core/` ✗→ 硬编码 `/data/commerce|research`（R3）· compose 交叉挂卷/缺双栈（R4）
- `core/` ✗→ 硬编码存储引擎（R5，仅 `core/storage/` 可 import duckdb）
- `plugins/*` ✗→ 直连指标视图取数（R6，唯一取数端点 = Semantic API）

## 运行

### Windows 快速开始

1. 执行 `git clone https://github.com/luc38545-stack/traceable-metrics.git`，进入项目根目录。普通界面试用也可以下载 ZIP，但 ZIP 不含 Git 版本信息，不能通过 `traceable-analysis` Skill 的版本自检。
2. 安装 Python 3.12 或更高版本（requirements-dev.lock.txt 锁定的 numpy/scipy 要求 >=3.12），并在安装器中勾选 **Add Python to PATH**。
3. 双击 `setup.bat`。脚本会在项目目录创建 `.venv`，安装运行依赖和完整测试依赖；首次安装需要网络。
4. 双击 `启动工作台.bat`。浏览器打开 `http://127.0.0.1:8765` 后即可使用商用轨工作台；关闭批处理窗口会停止服务。
5. 双击 `ci_check.bat` 运行架构门禁、运行门禁和 pytest 全量测试。

启动器优先使用项目内 `.venv`，也支持直接使用 PATH 中的 `python` 或 Windows `py -3`。Linux/macOS 用户可执行等价的命令行步骤，批处理文件仅适用于 Windows。

```powershell
# 0) 安装依赖（Windows 也可以在 PowerShell 中执行）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 1) 一键自检（批次 D 单一 CI 入口：架构门禁 + 运行门禁 + pytest 全量）
.\.venv\Scripts\python.exe ci_check.py

# 2) V1 端到端（真实 CSV → dbt 建模 → 快照 + 台账）
.\.venv\Scripts\python.exe -m plugins.commerce.run_v1 --csv examples/测试数据-8月26日订单.csv

# 3) 本地操作界面（主旅程：投料→体检→报告→对账）
.\.venv\Scripts\python.exe serve_cli.py
#   浏览器 http://127.0.0.1:8765
#   报告页提供“结果概览 / 报告预览”选项卡；预览可选，下载按钮始终可直接使用。
#   分享所需的文件名、服务器副本路径和指纹显示在预览下方，不写入导出报告正文。

# 4) 论文轨（face=R 独立入口，独立端口 8766 / 独立 CSRF / 独立卷）
.\.venv\Scripts\python.exe -m plugins.research.webapp_r
#   浏览器 http://127.0.0.1:8766 · 主旅程：上传 队列CSV+数据集清单+预注册台账 → 选计划 → run → 导出

# 5) 论文轨 CLI（无界面跑一次全链路）
.\.venv\Scripts\python.exe -m plugins.research.run_research --csv examples/cohort_20260831.csv --manifest examples/research_dataset_manifest.json --prereg examples/prereg_cohort_example.json --prereg-id pre-cohort-20260831
```

`python -m ...` 命令必须在项目根目录执行。若系统中存在多个 Python，优先使用安装脚本创建的 `.venv` 解释器；Linux/macOS 对应路径为 `.venv/bin/python`。

## 依赖

| 项 | 位置 / 版本 | 状态 |
|---|---|---|
| Python | 3.12+ | 必需（开发/测试锁文件约束；运行时未在 3.10-3.11 实测） |
| 运行依赖 | `requirements.txt` | `setup.bat` 自动安装 |
| 测试依赖 | `requirements-dev.txt` | `setup.bat` 自动安装 |
| 网络 | 首次安装时访问 PyPI | 必需 |

依赖安装在项目目录的 `.venv` 中，不会写入系统 Python。运行数据会在首次启动后生成到本地 `data/`，该目录不包含在 GitHub 发布包中。

## 两个真实故障（已由启动器规避，手动启动时务必注意）

**故障 1 · 全局 HTTP 代理劫持本机回环（D12 真人测试倒在这里）**
本机环境变量存在 `http_proxy=https_proxy=127.0.0.1:<随机端口>` 且 `NO_PROXY` 为空。
后果：访问 `127.0.0.1:8765` 的请求被塞进代理，返回 404/502 等**随机**错误，
表现为「页面有时打不开、导出按钮点了没反应」——极难归因，因为它看起来像代码 bug。
规避：启动器在进程内清除代理变量；手动启动请先执行
`set NO_PROXY=127.0.0.1,localhost,<-loopback>`；用 curl 自测必须加 `--noproxy '*'`。

**故障 2 · 多实例共存导致请求随机分发**
Windows 的 `SO_REUSEADDR` 允许第二个进程「成功」绑定已被占用的 8765，
于是新旧两份代码同时监听，请求随机落到其中一个 → 新功能时有时无（例如导出接口 404）。
规避：启动器先探活，已有健康实例就复用并拒绝起第二个。

## 验收记录（V1 DoD · 技术侧）

| 项 | 结果 |
|---|---|
| 端到端链 | 最新 run 见 `data/runs/`（当前最新 r-20260831-005312-203c）：raw 落地→体检→**断言闸门**→dbt 建模→语义编译→指标查询→快照 全绿 |
| 台账 | SQLite `data/runs/ledger.db`（runs + queries 两表）+ JSON 人读双写 |
| 快照 | `data/commerce/snapshots/s-*/commerce.db`（sha256 记录在台账；快照含嵌套指标视图，可重放对账） |
| 指标 | pay_success_rate@v2（嵌套指标：pay_success_orders / pay_attempted_orders），演示数据期望 7/8≈0.875（订单数口径，独立复算核对） |
| 建模 | dbt 声明式（`dbt/models/dwd_base.sql`），从不可变 raw 批次读取（P3：建模不读上传临时文件） |
| 门禁 | R1-R7 PASS；负面测试 **16/16**（N1-N16，见 `tests/test_negative.py`） |
| 统计腿 | `core/statistics/`（checks 前提检查 / diagnosis 诊断书 / executor 执行器）已落地：12/12（S1-S12，见 `tests/test_statistics.py`） |
| A/B 实验 | `plugins/commerce/ab_test.py` + `core/experiment/hashing.py` 已落地：**19/19**（A1-A18，见 `tests/test_ab.py`）；分流跨进程稳定、均匀性偏差 <1.5%、SRM(α=0.001) 拦截至上、peeking 走 Bonferroni、护栏含 NO_DATA 态 |
| LLM 解释腿 | `core/copilot/`（provider 可换层 / claims_gate D6 闸门 / routing 路由表 / explain 生成器）已落地：**14/14**（C1-C14，见 `tests/test_copilot.py`）；双形态输出（正文+claims）、幻觉/抄错数字拦截、Provider 未配置显式失败（P4）；**2026-08-31 已接线 webapp**（`/api/explain` + 导出报告 LLM 解释段，见下节施工补记） |
| 断言闸门 | **§08.1 三级断言引擎**（2026-08-31 施工）：L1 行数突变带 ±40% 熔断（7 次运行均值基线）/ L2 唯一·非空·枚举·引用完整 / L3 跨源对账·环比合理性；17/17（`tests/test_assertions.py`）；run_v1 CLI + webapp 双管道接入，blocking 失败 `AssertionBlocked` 红灯封下游并显式登记 |
| 对账 | ADR-18：claims 与快照库重放 diff，篡改 value → FAIL 且报告不渲染；新口径下 reconcile_diff=clean |
| 权限 | ToolRegistry 级别白名单 + per-key 白名单（ADR-17 L2 / S2 判据，N9/N11/N15/N16）；per-key 白名单**绑定轨道**（审计 #8，跨轨调用必拒）；**2026-08-31 已接线运行时**（`/api/agent` + `/api/agent/keys` 看板 + `agent_tool_call` 审计，见下节施工补记） |
| 资产归属 | **ADR-12 已落地**：`core/registry/assets.py` 以 P8' 三问判定 `shared/commerce/research` scope，`infra/registry/assets.json` 登记现有关键资产；重复、越界路径和历史篡改均显式拒绝；回归见 `tests/test_asset_registry.py` |
| 契约先行 | **D11 已落地**：`core/governance/contracts.py` 比较源契约变更，删除/类型变化/收紧可空性必须有评审且提前至少 14 天；基线见 `infra/contracts/source_catalog.json`，回归见 `tests/test_contract_governance.py` |
| 敏感数据治理 | **D7 已落地**：`core/governance/sensitivity.py` 四级分类（public/internal/confidential/restricted）+ 显式策略覆盖 + 未知字段默认 internal + 按可见级别动态脱敏；原始记录不改写，受限字段默认 token 化；策略见 `infra/governance/sensitivity_policy.json`，回归见 `tests/test_sensitivity.py` |
| 备份与恢复 | **D9 已验证完成**：`core/governance/backup.py` 双轨备份、manifest 大小/SHA-256 校验、暂存后原子恢复、目标防覆盖与跨轨/篡改/路径穿越拒绝；回归已通过，见 `tests/test_backup_restore.py` |
| S3 论文轨 | **2026-08-31 施工**：预注册（`core/statistics/prereg.py`，append-only + 计划指纹逐字对账）/ 数据集四要素+IRB gate / k-匿名 + DP 预算账本 / 复现包（篡改诚实检出）/ Quarto `[R]` 出版 / 端到端管道 `run_research.py` / **face=R 独立入口 `webapp_r.py`（8766，独立 CSRF+卷）**；21/21（R1-R21，`tests/test_research_track.py`）+ 18/18（W1-W18，`tests/test_research_webapp.py`）；CLI+HTTP 端到端实测：GREEN / counts=(325,650,217,650) / reconcile clean / 复现包+Quarto 闭合。**2026-08-31 余项闭环**：登记册冻结开关（R23-R26）/ dbt `--select dwd_cohort` 接线（R22）/ count 族执行器 poisson+negative_binomial（R27-R30、S13-S14） |
| S4 双轨合龙 | **2026-08-31 施工**：导出桥五闸（`core/bridge/`，人审 token→源出口→受控中转→目标入口→双端审计，含反向路径不存在性，14/14 `tests/test_bridge.py`）/ **face=A 控制面 `plugins/admin/webapp_a.py`（8767，15 分钟会话 + CSRF + 导出二次确认，12/12 `tests/test_admin_webapp.py`）** / ADR-16 config overlay（`core/config/overlay.py`，7/7 `tests/test_overlay.py`）/ 双轨健康总览 + 部署 diff 预览 / **双轨隔离红队 9/9（`tests/test_redteam.py` R31-R35）** / **Docker 运行时隔离 DKR1-DKR6 6/6（2026-09-02 已实测）** |
| 审计修复 | 2026-08-30 依审计 15 项清单逐项闭环，见下节「审计整改记录」 |

## 审计整改记录（2026-08-30 · 依最终审计 15 项清单闭环）

> 审计结论原文：**「这是一个能够真实运行的 commerce 单轨技术原型，不是严格符合 REV C.2 的双轨 V1 平台。」**
> 以下 15 项全部修复并经全量回归 + 端到端实测，V1 仍为 commerce 单轨（论文轨 S3 未启动）。

| # | 审计项 | 修复落点 | 验证 |
|---|---|---|---|
| 1 | claims NaN/Inf 绕过对账（float('nan') 恒 False 被误判 PASS） | `core/claims/reconcile.py`：validate_claim 加有限数校验；replay 非有限值显式 FAIL | N6 仍拦截篡改；对账套件全绿 |
| 2 | claims source 未强绑定真实查询（曾用 `replay:xxx` 伪 id 顶替） | `plugins/commerce/webapp.py`：query_id 缺失 → 显式报错，不再伪造 | e2e 4 对账 passed |
| 3 | 同日快照互相覆盖（`s-{日期}` 同名目录） | `run_v1.py` + `webapp.py`：快照目录 = `s-{run_id短号}`，逐 run 唯一 | 实测 `s-20260830-213737-839e` 与 run_id 对齐 |
| 4 | 台账 `INSERT OR REPLACE` 静默覆盖，非 append-only | `core/audit/ledger.py`：改纯 INSERT，重复 run_id → LedgerError | N8 仍拒收缺 provenance；重复写入抛错 |
| 5 | provenance 缺代码版本（commit_sha） | `ledger.py` 新增 `build_digest()`（源码树 sha256，仓库无 git 的不可变指纹）+ REQUIRED_PROVENANCE 增 commit_sha；run_v1/webapp 落盘；kernel 页脚展示 | 实测台账 commit_sha 完整落盘、报告页脚可见 |
| 6 | ADR-13 网络隔离违规（两轨同网） | `infra/compose.yml`：commerce-net / research-net 拆分 + bridge-net 预留 | R4 网络检查通过 |
| 7 | HTTP 无 Origin/Host 防护 | `webapp.py`：跨源请求/伪造 Host → 403 | 实测 evil Origin=403、evil Host=403、正常=200 |
| 8 | per-key 白名单未绑轨道（跨轨越权） | `core/auth/tool_registry.py`：register_key 必填 tracks，动作×轨道双重校验 | N15（跨轨拒）+ N16（本轨放行）新增 |
| 9 | `land_file` entity 未消毒（`../` 逃逸卷） | `core/ingestion/land.py`：entity 白名单正则 `^[A-Za-z0-9][A-Za-z0-9_-]*$` | 非法 entity 抛 ValueError |
| 10 | 上传索引可指向卷外路径 | `webapp.py` `_find_upload`：resolve 后过 `assert_inside_volume` | 卷外路径 → PermissionError |
| 11 | 并发共享 DuckDB/dbt 目标库互踩 | `webapp.py`：`action_run` 串行化（进程级互斥锁） | e2e 12/12 |
| 12 | 快照缺失时拿任意快照顶替 | `webapp.py` `action_claims`：精确快照缺失 → 显式报错 | e2e 对账 passed |
| 13 | 报告 track 缺失静默回落 commerce | `core/report/kernel.py`：build_conclusion/render_html 均显式拒绝（ReportBlocked） | N13 仍拦截对账失败 |
| 14 | 快照溯源三元组缺 commit_sha（同上 #5） | 同上 #5 | 同上 |
| 15 | R4 门禁只查卷不查网 | `tests/check_dependencies.py`：新增网络检查（轨道服务必须显式声明网络、不得上 bridge-net、两轨不得同网） | 用旧布局（同网）实测被抓出违规 |

## 施工补记（2026-08-31 · V1 核心三缺口整改）

按架构书补齐 V1 范围内三个此前「未实现/未接线」的缺口（对照审计报告第八节），全部先加失败用例再改实现：

| 缺口 | 落点 | 实证 |
|---|---|---|
| §08.1 三级断言引擎 | `tests/test_assertions.py`（17 用例，先 RED）→ `core/quality/assertions.py`（`Assertion`/`AssertionEngine`/`AssertionBlocked` + 5 工厂）→ `plugins/commerce/assertions_hook.py`（app 层桥接）→ run_v1 + webapp 双管道 | 干净 CSV 4 断言 0 failed；重复 order_id 污 CSV → L1+L2 双重熔断、失败显式登记 |
| Capability Matrix 运行时接线 | `webapp.py`：`/api/agent` + `/api/agent/keys`（看板掩码）+ `TRACEABLE_AGENT_KEY` env 注册 + `agent_tool_call` 审计 | 未注册 key 403；L1 `read_via_semantic` 放行（reconcile_diff=clean）；L1×`pipeline_rerun` 403 |
| Copilot 接线（V1-DoD） | `webapp.py` `action_explain` + `/api/explain` + 导出报告 `llm_explanation` 透传 | 未配置 → `not_configured`；未就绪 → `unavailable`+reason；ok 路径 mock 实测过（narrative 渲染实际值） |

**争议点坐标注**（完整清单见审计报告第八节，共 9 条）：行数基线取 7 次运行均值 ±40%（架构书未规定口径）、枚举默认 non-blocking、断言清单 V1 用代码注册 + JSON 兜底（YAML 声明式进 backlog）、看板只读（白名单写入唯一途径 = env）、V1 单 key L1、Provider 未就绪显性 unavailable——均为如实标注的实现取舍，非伪装完成。

## 施工补记（2026-08-31 晚间 · S3 论文轨）

按修改意见 §六「S3 论文轨」清单施工，测试先行 RED→GREEN，全量回归 188→**206 passed**：

| 组件 | 落点 | 实证 |
|---|---|---|
| 预注册 | `core/statistics/prereg.py`（`AnalysisPlan`/指纹/逐字对账/append-only 台账）+ `plugins/research/prereg_hook.py`（未登记拦截） | R1-R4（含重复 plan_id 拒覆盖、口径不一致点名） |
| 数据集 gate | `plugins/research/dataset_gate.py`（四要素 + IRB 过期/未批准/缺要素点名） | R5-R8 |
| 隐私核 | `core/privacy/kanonymity.py`（k-匿名报告/断言，列名 R9 安全）+ `core/privacy/dp_budget.py`（DP 账本 append-only） | R9-R14 |
| 复现包 | `core/repro/package.py`（provenance 四元组拒缺、篡改诚实检出） | R15-R17 |
| 端到端管道 | `plugins/research/run_research.py`（gate→快照→台账双写→复现包→Quarto，失败显性登记） | R18-R21（count 族显式失败 / research_mode 诊断 / `[R]` 出版 / 650+650 全链路） |
| face=R 独立入口 | `plugins/research/webapp_r.py`：端口 8766、独立 CSRF、独立卷、上传三件套→选计划→run→报告→导出（qmd/repro 带 sha256） | W1-W18（含无 token 403、跨源 403、索引损坏 P4、并发 409、幂等键、失败登记审计） |

**S3 余项（2026-08-31 已全部闭环，测试先行 RED→GREEN）**：
- ① research-methods 登记册**冻结开关**：`core/statistics/method_contract.py`（R23-R26：冻结后注册被拒 / 指纹稳定 / run 台账记录方法集指纹）；
- ② run_research 建模走 dbt `--select dwd_cohort`：`run_research.py:149` 接 `run_dbt_model(select="dwd_cohort")`（R22，同一 dbt 工程按 select 隔离）；
- ③ count 族执行器：`core/statistics/executor.py` 新增 `poisson_test` + `negative_binomial_test`（NB2 对数链接 GLM + offset=log(exposure)），`analyze()` 接线 + **`replay_stat` 补 poisson/negbin 分支**（修复 count 族对账基准为空的隐蔽断链；R27-R30 / S13-S14）。

## 施工补记（2026-08-31 · S4 双轨合龙 + S3 余项闭环）

全量回归 206→**259 passed**（+53：bridge 14 / overlay 7 / face=A 12 / 红队 9 / research R22-R30 / statistics S13-S14），架构门禁 R1-R9 与运行门禁 RT1-RT6 双 PASS：

| 组件 | 落点 | 实证 |
|---|---|---|
| 导出桥五闸 | `core/bridge/bridge.py`：闸1 人审 token（一次性/15 分钟 TTL/purpose+轨向校验）→ 闸2 源台账 run+快照 sha256 → 闸3 受控中转非轨卷 → 闸4 目标 imports/ 落盘+sha256+manifest → 闸5 双端审计（先审计后交付，失败回滚）；**反向路径不存在性**（目标轨 ctx 读源轨卷 → PermissionError） | B1-B15 14/14（逐闸负面 + 正向全流程双端事件+sha 一致） |
| face=A 控制面 | `plugins/admin/webapp_a.py`（8767，ADR-15 第三面）：token 登录（未配置显式 not_configured）/ 15 分钟会话单调过期 / CSRF+Origin+限速 / 双轨健康总览 / 部署 diff 预览 / 导出桥二次确认 confirm=true（P1-04） | A1-A12 12/12（含 HTTP 层 403/401） |
| ADR-16 overlay | `core/config/overlay.py` + `infra/config/*.yml`：deep_merge / 类型漂移（bool 独立、int/float 归 num、新增 key 不算）/ shared.* 冲突 / 对照册来源标注 / diff 预览 | O1-O7 7/7 |
| 双轨隔离红队 | `tests/test_redteam.py`：R31 core 跨轨路径零违例（C-4）/ R32 plugins 互引零违例（C-2）/ R33 跨轨卷访问双向 PermissionError / R34 绕过导出桥直连被拒（无 token BridgeGateError + 跨轨取数 SemanticQueryError）/ R35 dbt 模型串门防护（调用点必须显式 select 且与轨匹配） | R31-R35 9/9 |

**Docker 层双栈隔离**：compose 双栈 + R4 门禁（卷单轨挂载、双网络、bridge-net 禁入）静态 PASS；Docker Desktop 4.89.0（Engine/CLI 29.7.2、Compose v5.5.0）于 2026-09-02 完成真实验收。首次实测 RED 5/6 暴露两服务重启循环，修正为导入检查成功后常驻，再测 DKR1-DKR6 **6/6 PASS**：容器 0 重启、实际 Mounts 单轨、实际 Networks 互斥、bridge-net 未创建、双向对侧路径 `FileNotFoundError`、双向 DNS 不可解析。验收结束已自动清理容器与网络。

**D6 审计留存策略**：`infra/config/base.yml` 固定 `shared.audit_retention_days: 400`；`validate_audit_retention_policy()` 在配置加载和双轨合并时强制严格整数且 ≥400，任一轨 overlay 仍不得覆盖 shared 策略。D6-1～D6-7 **7/7 PASS**；缺失、399、bool、字符串均显性拒绝。R8 同时保证应用内审计台账只追加、不执行清理。

```powershell
python tests/docker_isolation_acceptance.py
```

**Agent key 使用**（用户自填，不落盘）：

```
# Windows 临时设置（当前窗口有效）
set TRACEABLE_AGENT_KEY=ak-你的密钥
python serve_cli.py
# 之后：GET /api/agent/keys 看板；POST /api/agent 带 {"key_id","action","track"}
```

> 注：POST 需带 `X-CSRF-Token`（从 `GET /api/session` 取）。LLM 解释环境变量：`TRACEABLE_LLM_BASE_URL`（默认本机 OmniRoute 127.0.0.1:20128/v1）/ `TRACEABLE_LLM_API_KEY` / `TRACEABLE_LLM_MODEL`。

回归结果：R1-R7 PASS · N **16/16** · S 12/12 · A 19/19 · C 14/14 · e2e **12/12**。
遗留（诚实记录，非本次清单项）：D12 face=C 前两次真人测试因真实易用性缺陷判定 FAIL；
整改后真实 Chromium 主旅程 28/28 PASS，第三次真人复测于 2026-09-02 由用户明确确认 **PASS**。
face=R 真人验收已于 2026-09-05 由用户确认通过；结果预览、报告预览、Quarto 与复现包下载均已验收。审计 #7 已做最小防护但无会话/CSRF token 体系
（本机单用户场景够用，V1.2 需接认证）；#11 以串行化规避，未做分轨独立库。

用户侧验收（真人 30 分钟主旅程，D12）：**face=C 与 face=R 均已通过**。
两次 face=C FAIL、RED→GREEN 整改证据及第三次 PASS 见 `tests/d12/observation_sheet.md`；
face=R 真人验收记录见 `tests/d12/observation_sheet_research.md`；自动化预演不替代真人确认。

## 已知边界（诚实记录）

- `health.py` 的日期混格式检测为启发式（V1.1 改为格式枚举统计）。
- 语义编译器 V1 支持 ratio/sum/count/avg 四型 + 嵌套指标引用（分子/分母引用子契约视图）；
  window/funnel 等在 V2+。
- 统计执行器已落地 **Python 核**（`core/statistics/executor.py`）：
  `two_proportion_test` / `welch_t_test` / `mann_whitney` / `fisher` 四法；
  RED 拒出 p 值、YELLOW 强制未检项清单 + 敏感性检验、结论数字生成 claims 并以**重算**对账。
  未做：logistic / propensity_score（调用会明确报「登记册有、执行器未实现」，不静默降级）；
  poisson / negative_binomial count 族已落地并通过 R27-R30、S13-S14 回归；
  R 核未接（架构书 R/Python 双核的另一半）。
- A/B 实验插件（`plugins/commerce/ab_test.py`，S2 判据项）已落地，主分析复用上述执行器（不另起炉灶）。
  已知边界：
  - peeking 校正**只实现 Bonferroni**（最保守，牺牲功效换安全）。序贯方法
    （O'Brien-Fleming / alpha spending）属进阶选项，不在 V1 范围。
  - SRM 只做总体卡方拟合优度，不做逐日/逐维度下钻（细分 SRM 更能定位埋点问题，V1.2 考虑）。
  - 分流是**纯函数**：只接收 unit_id 列表并分桶，不接数据库、不落盘。真实埋点侧的
    分桶一致性（服务端与客户端算出来的桶要一样）需在上游保证，本模块只保证「同输入必同输出」。
  - 护栏判定用阈值法（Δ 是否超过 threshold），**不做护栏指标自身的显著性检验**——
    避免把「没检出显著恶化」误读成「确认没恶化」。
- 前提检查器的**独立性**与**缺失机制（MCAR/MAR/MNAR）**刻意标为 `auto=False`：
  这两项无法仅从观测数据判定（D5 统计诚实），一律进未检项清单，不伪装成"已通过"。
- compose 双栈拓扑已完成 Docker 运行时隔离验收；当前容器作为常驻拓扑探针，完整产品容器化运行仍在 V1.2 启用。
- LLM 解释腿（`core/copilot/`，V1-DoD 技术侧「LLM 解释(claims 过闸)」）已落地：**双形态输出**
  （自然语言正文 + claims 数组，ADR-18 / P0-2 盲区闭合）+ D6 数字对账闸门（幻觉/抄错数字
  一律拦截并出失败单）+ Provider 可换层（宪.1，环境变量 `TRACEABLE_LLM_BASE_URL` /
  `TRACEABLE_LLM_API_KEY` / `TRACEABLE_LLM_MODEL`，key 由用户自填）。
  已知边界：
  - **默认不读环境代理**（本机全局代理劫持回环是已知故障）；真实外部 API 如需代理，
    需在 Provider 之上自行包装。
  - 数字提取器只抓「小数/百分比」token，裸整数（「第2名」「2026 年」）不抓——
    统计结论数字几乎总是小数/百分比形态，V1.1 再做单位感知提取。
  - 交叉审核（第二模型审第一模型）与人审升级：V1 **显式标注未启用**（P4），
    audit 字段暴露给消费端，等接入真实 Provider 后在框架内补齐。
  - RED 结论（无 claims）→ 直接拒出解释（C-5：无对账依据，LLM 不可能是事实源）。
  - 接线点：报告渲染前调 `core.copilot.explain()`（产物自带 audit，可随报告展示
    「本解释未经交叉审核」）；真实调用需用户先配置上述环境变量。
- 论文轨（S3）已施工（2026-08-31）：预注册 / 数据集 gate / k-匿名 / DP 预算 / 复现包 / Quarto / face=R 入口全链路闭合；
  **S3 余项 2026-08-31 全闭环**：登记册冻结开关（R23-R26）/ count 族执行器 poisson+negative_binomial（R27-R30、S13-S14）/ run_research 建模走 dbt `--select dwd_cohort`（R22）。
- 双轨合龙（S4）2026-08-31 已施工：导出桥五闸 + 反向路径不存在性（14/14）/ face=A 控制面 8767（12/12）/ ADR-16 overlay（7/7）/ 双轨健康总览 + 部署 diff / 双轨隔离红队（9/9，四攻击面全被拒）——D12 报告可读性与可选预览整改后常规回归 **279 passed**；**Docker 真实双栈隔离于 2026-09-02 实测 DKR1-DKR6 6/6 PASS**。

### 2026-09-05 续接状态（以现场检查为准）

- 当前工作区不是 Git 仓库；8765/8766/8767 均未启动。
- pytest 当前可收集 **284 项**。研究轨投料页已补充中文用途说明；上传层已显式校验 `participant_id`、`arm`、`converted`，并专门拒绝订单 CSV 误传到研究轨。
- W20/W21 与新增 CSV 契约用例已通过；需要系统临时目录写入的研究网页/研究轨测试受 Windows `WinError 5` ACL 阻塞，不能据此宣称全量通过。
- face=R D12 真人验收已通过；结果预览、报告预览、Quarto/复现包下载均已完成验收，记录见 `tests/d12/observation_sheet_research.md`。
- 续接更新：用户已确认 face=R D12 真人验收通过；结果预览、报告预览、Quarto 下载和复现包下载均完成验收。
- 历史 run 台账 `r-20260827-222645-2ceb` 为金额口径时代产物（无 metric_series 值），
  新口径起（v2）统一为订单数口径；旧快照 `s-20260827/28/29` 保持原样可查，不再写入。
