# Skill v0.1.3 验收清单（CHECKLIST）

> 每次迭代 Skill 后必须重跑本清单；结果记录在表格与 README「已验证范围」。

## A. 环境自检

- [ ] `TRACEABLE_HOME` 未设置时：check_env 退出码 1，JSON 报缺路径 ✅
- [ ] 路径指向非主项目目录时：报关键文件缺失 ✅
- [ ] 主项目 HEAD 不在 pinned tag 时：version_pin 失败并给出 checkout 指引 ✅
- [ ] 未跟踪源码、配置或指标契约存在时：worktree_clean 失败 ✅
- [ ] `.gitignore` 明确排除的 `_review*/` 测试目录不影响自检 ✅
- [ ] 全部就绪时：退出码 0，`"ok": true` ✅

## B. 管道（真实 CSV 端到端）

- [ ] `run_v1 --csv examples/orders_20260826.csv` 成功产出台账 JSON
- [ ] 台账含 `schema_version: 1`
- [ ] 台账 provenance 四元组 + snapshot_sha256 齐全

## C. claims 与对账

- [ ] `claims_dump --run <run_id>` 产物 `reconcile_diff: clean`
- [ ] 未知 run_id：显性报错（exit 1）

## D. D6 闸门

- [ ] 数字与 claims 一致 → exit 0
- [ ] 正文掺入未授权数字 → exit 2 + 失败单
- [ ] 缺 provenance / 不支持 schema → exit 3 / 4

## E. 失败路径（Agent 行为）

- [ ] check_env 失败后 Agent 终止，不跑管道
- [ ] 对账不 clean 时无任何结论性数字输出
- [ ] D6 FAIL 的正文未出现在交付中

## F. 版本与仓库

- [ ] SKILL.md 版本声明与 git tag 一致（skill-v0.1.3 / traceable-v1.0.3）
- [ ] 主项目 tag 存在且 CI ALL PASS 后才打
- [ ] README 安装表可照做（无本机绝对路径）

## 验证记录

| 日期 | 项目 | 结果 |
|---|---|---|
| 2026-09-20 | A 环境自检六项 | ✅ 本机（含负向） |
| 2026-09-20 | B 管道端到端（demo CSV） | ✅ 本机 |
| 2026-09-20 | C claims 对账 | ✅ 本机 |
| 2026-09-20 | D D6 闸门 9 项回归 | ✅ 本机（342 passed CI 全绿） |
| — | E Agent 行为 | 部分：失败路径由 D 覆盖，真人多 Agent 验证待做 |
| — | F 跨机器验证 | ❌ 待非开发机实测（阶段 4） |
