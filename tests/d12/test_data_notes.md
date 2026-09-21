# D12 测试数据说明

## 用哪个文件

`examples/测试数据-8月26日订单.csv`（8 行业务语言演示数据）。

## 为什么这个文件合规（契约字段不本地化）

D12 铁律：业务语言 CSV **不得本地化契约引用字段**——本地化会让编译出的过滤条件静默失效、指标静默变错。

本文件字段检查（对照 `schemas/metrics/commerce/` 契约）：

| 列 | 本文件 | 契约引用 | 是否可本地化 |
|---|---|---|---|
| 订单编号 | 订单编号 | 无（grain 语义） | ✅ 已本地化 |
| created_at | created_at | `time_column` | ❌ 保持原样 |
| 销售渠道 | 销售渠道 | `queryable_dimensions`（元数据，不参与过滤） | ✅ 已本地化 |
| amount_total | amount_total | `pay_attempted_orders` 等计数契约不引用金额 | ❌ 保持原样（保守） |
| amount_paid | amount_paid | 同上 | ❌ 保持原样（保守） |
| status | status（枚举 `paid`/`cancelled`） | `pay_success_orders` 过滤 `status = 'paid'` | ❌ **枚举值严禁本地化**（已支付/已取消 会使过滤失效） |

## 验证方法（每次测试前跑一遍）

```powershell
python -c "import sys; sys.path.insert(0, r'<仓库根>'); from plugins.commerce.run_v1 import run; from pathlib import Path; r = run(Path(r'<仓库根>\examples\测试数据-8月26日订单.csv')); print(r['metric_series'])"
# 期望输出: [['2026-08-26', 0.875]]   （订单数口径 7/8；若出现 0.8521 即金额口径回退，禁止测试）
```

## 若需自制测试数据（给未来的业务场景）

1. 复制本文件改内容，**只动业务列**（订单编号/销售渠道），不动 `created_at`/`amount_total`/`amount_paid`/`status` 及其枚举值。
2. 按上面验证方法跑一遍，确认指标值符合你的手工复算。
3. 手工复算口诀：成功率 = status 为 paid 的行数 ÷ 全部行数（本演示数据无测试单标记）。
