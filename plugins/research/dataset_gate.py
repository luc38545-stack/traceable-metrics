"""研究数据集四要素 + IRB 钩子（S3 论文轨 · 架构书 §六「研究数据集四要素/IRB 钩子」）。

论文轨与商业轨的本质差异：数据来自人（参与对象），必须回答
「这数据是什么、谁负责、怎么来的、伦理审批在哪」——四要素缺一不可：

- dataset_name   数据集名称（可寻址）
- version        版本（不可变数据版本的入口）
- owner          数据责任人（可追责）
- collection_method  采集方式/来源说明（可审计，是否已脱敏在此声明）

外加 IRB 块：approved（是否批准）、expires_at（有效期）、approval_no（批件号）。

规则：
- 缺要素 → DatasetGateError 点名全部缺失项（不只第一个）；
- IRB 未批准 → DatasetGateError；已过期 → IrbExpired（子类，语义更精确）；
- 通过 → 返回规范化报告 {"passed": True, ...}，供台账记录与下游展示。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

#: 四要素（必填）
REQUIRED_ELEMENTS = ("dataset_name", "version", "owner", "collection_method")
#: IRB 块必填键
IRB_REQUIRED = ("approved", "expires_at", "approval_no")

__all__ = ["DatasetGateError", "IrbExpired", "check_dataset_gate"]


class DatasetGateError(ValueError):
    """研究数据集清单不合法（缺要素 / IRB 未批准 / 结构非法）。"""


class IrbExpired(DatasetGateError):
    """IRB 已过期——论文轨不可用过期伦理审批跑数据。"""


def _parse_date(v: Any) -> date:
    """ISO 日期解析；非法 → DatasetGateError。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return datetime.strptime(v.strip(), "%Y-%m-%d").date()
        except ValueError as e:
            raise DatasetGateError(f"IRB.expires_at 必须是 ISO 日期（YYYY-MM-DD）：{v!r}") from e
    raise DatasetGateError(f"IRB.expires_at 必须是 ISO 日期：{v!r}")


def check_dataset_gate(manifest: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """校验研究数据集清单。通过 → 规范化报告；不通过 → 显式异常。

    manifest: 数据集清单 dict（含四要素 + irb 块 + 可选 privacy 块）。
    today: 参照日期（测试注入；默认 date.today()）。
    """
    if not isinstance(manifest, dict):
        raise DatasetGateError(f"数据集清单必须是 dict，实为 {type(manifest).__name__}")

    missing = [e for e in REQUIRED_ELEMENTS if not manifest.get(e)]
    if missing:
        raise DatasetGateError(
            "研究数据集缺要素：" + "、".join(missing)
            + "（四要素=名称/版本/责任人/采集方式，缺一不可）"
        )

    irb = manifest.get("irb")
    if not isinstance(irb, dict):
        raise DatasetGateError("研究数据集缺少 irb 块（伦理审批，论文轨强制）")
    irb_missing = [k for k in IRB_REQUIRED if k not in irb]
    if irb_missing:
        raise DatasetGateError("IRB 块缺字段：" + "、".join(irb_missing))

    if irb.get("approved") is not True:
        raise DatasetGateError(
            f"IRB 未批准（approval_no={irb.get('approval_no')!r}）——"
            "论文轨禁止用未批准的数据发表结论"
        )

    expires = _parse_date(irb["expires_at"])
    ref = today or date.today()
    if expires < ref:
        raise IrbExpired(
            f"IRB（{irb.get('approval_no')}）已于 {expires.isoformat()} 过期"
            f"（参照 {ref.isoformat()}）——请先更新伦理审批再继续"
        )

    privacy = manifest.get("privacy") or {}
    return {
        "passed": True,
        "dataset_name": manifest["dataset_name"],
        "version": manifest["version"],
        "owner": manifest["owner"],
        "collection_method": manifest["collection_method"],
        "irb": {
            "approved": True,
            "expires_at": expires.isoformat(),
            "approval_no": irb["approval_no"],
        },
        "privacy": dict(privacy),
    }
