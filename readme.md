# TraceableMetrics · 可信数据分析平台

把一份 CSV 变成「带快照、带台账、数字可追溯到原始文件」的指标结论——每一步由代码闸门验证，任何结论都能回答三个问题：数字从哪来、用什么代码算的、能不能重放对账。

## 产品说明

TraceableMetrics 是一个本地运行的数据分析自动化平台，围绕一条核心纪律构建：**LLM 与人不产生关键数字，只解释台账里已经对账过的数字**。

一次分析的完整链路：

```
CSV 投料 → 体检 → 断言闸门 → dbt 建模 → 指标查询 → 不可变快照 → 运行台账
        → claims 导出 → 数字对账 → D6 解释闸门 → D7 脱敏交付
```

每一环失败都会显性终止并留下失败记录，不存在"部分成功"的中间态。

## 快速开始

环境要求：Windows + Python 3.12+（安装时勾选 **Add Python to PATH**），首次安装需访问 PyPI。

1. 克隆仓库并进入根目录：

```bash
git clone https://github.com/luc38545-stack/traceable-metrics.git
cd traceable-metrics
```

2. 双击 `setup.bat`——在项目目录创建独立 `.venv`，按 `requirements.lock.txt` / `requirements-dev.lock.txt` 安装锁定版本的依赖，不写入系统 Python。

3. 跑一次端到端演示（商用轨，中文订单 CSV）：

```powershell
.\.venv\Scripts\python.exe -m plugins.commerce.run_v1 --csv "examples/测试数据-8月26日订单.csv"
# 成功输出末行：== DONE · run 台账 → data/runs/r-*.json
```

4. 一键自检（架构门禁 + 运行门禁 + pytest 全量）：

```powershell
.\.venv\Scripts\python.exe ci_check.py
```

Linux/macOS 用户可按等价命令行步骤操作（解释器路径为 `.venv/bin/python`）；`python -m ...` 命令必须在项目根目录执行。

## 双轨入口

| 轨道 | 用途 | CLI 入口 | 本地界面 |
|---|---|---|---|
| 商用轨（face=C） | 订单数据分析：指标、报告、A/B 实验 | `python -m plugins.commerce.run_v1 --csv <CSV>` | `serve_cli.py` → http://127.0.0.1:8765 |
| 研究轨（face=R） | 队列研究：预注册、数据集 gate、复现包、Quarto 出版 | `python -m plugins.research.run_research --csv <CSV> --manifest <清单> --prereg <计划>` | `python -m plugins.research.webapp_r` → http://127.0.0.1:8766 |
| 控制面（face=A） | 双轨健康总览、跨轨导出审批 | — | `python -m plugins.admin.webapp_a` → http://127.0.0.1:8767 |

两轨数据卷、网络与代码依赖完全隔离：`core/` 不知道数据住在哪，`plugins/commerce` 与 `plugins/research` 互不引用，跨轨导出必须经五闸导出桥（人审 token → 源对账 → 受控中转 → 目标落盘 → 双端审计）。

## 可信性保证

| 机制 | 说明 |
|---|---|
| 不可变快照 | 每次 run 对数据库做快照，SHA-256 写入台账；报告引用的每个数字都可重放验证 |
| 运行台账 | SQLite + JSON 双写，append-only；provenance 四元组（run_id / snapshot_id / batch_id / commit_sha）缺一即拒收 |
| 代码指纹 | 源码树（core/plugins/schemas/infra/dbt）+ 依赖锁整体 SHA-256；代码或锁文件任何改动都会改变指纹，老 run 无法冒充新代码版本 |
| 依赖锁定 | `requirements.lock.txt` 全量锁定（含传递依赖）；依赖摘要 = 锁文件 SHA-256，缺锁显式失败 |
| claims 对账 | 结论数字以 claims 结构导出，与快照库重放 diff；篡改任一数字 → FAIL 且报告拒绝渲染 |
| D6 解释闸门 | LLM/人写的解释正文逐位核对引用数字，幻觉或抄错 → 拦截并出失败单，未过闸文本不许交付 |
| D7 脱敏 | 四级敏感度分类（public/internal/confidential/restricted）+ 字段白名单；交付副本必须过确定性脱敏 CLI，原始输入永不改写 |
| 版本自检 | Skill 与运行门禁校验 git tag 固定、工作树干净；dirty 或无法判定（unknown）时拒绝作为正式复现产物 |

## Skill：traceable-analysis

`skills/traceable-analysis/` 提供可直接被 Agent 执行的分析 Skill：接收用户 CSV → 环境自检 → 跑管道 → 导出 claims → 起草解释并过 D6 闸门 → D7 脱敏交付。Skill 固定引用主项目 tag `traceable-v1.0.4`，版本不匹配即终止。

安装与执行步骤见 `skills/traceable-analysis/SKILL.md`；环境自检脚本为 `skills/traceable-analysis/scripts/check_env.py`，发布前自检为 `skills/traceable-analysis/scripts/quick_validate.py`。

## 测试

```powershell
# 架构门禁（纯静态，R1-R9：依赖方向/硬编码路径/网络隔离等）
python tests/check_dependencies.py

# 运行门禁（RT1-RT6：解释器版本/锁依赖安装/dbt CLI/版本精确比对）
python tests/check_runtime.py

# 全量测试（含架构门禁 + 运行门禁 + pytest）
python ci_check.py --skip-browser
```

测试覆盖：负面路径（篡改/越权/断链均显式拒绝）、统计执行器、A/B 分流、对账闸门、双轨隔离红队、Docker 双栈隔离验收、浏览器端到端等。发布契约测试（`tests/test_release_contract.py`）锁定依赖摘要、代码指纹与复现包的机器判据。

## 已知边界（诚实记录）

- 指标语义编译器支持 ratio / sum / count / avg 四型与嵌套指标引用；window / funnel 在 V2+。
- 统计执行器为 Python 核：two-proportion / Welch t / Mann-Whitney / Fisher / poisson / negative-binomial；logistic 与 propensity score 已登记但执行器未实现（调用显式报错，不静默降级）；R 核未接。
- A/B peeking 校正仅实现 Bonferroni（最保守）；SRM 只做总体卡方，不做逐维度下钻。
- 前提检查中的独立性与缺失机制（MCAR/MAR/MNAR）刻意标为不可自动判定，一律进未检项清单。
- 日期混格式检测为启发式实现。
- 交叉审核（第二模型审第一模型）显式标注未启用；LLM 解释默认不读环境代理。
- 浏览器自动化测试在部分 Windows 环境受系统 ACL 限制（WinError 5）；如遇阻塞请记录环境信息，不要为通过测试修改产品语义。
- 运行数据生成于本地 `data/`（不入库）；Linux/macOS 未纳入发布验证范围，当前验证基线为 Windows + Python 3.12。

## 许可证

MIT License，见 [LICENSE](LICENSE)。
