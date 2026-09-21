"""预注册（S3 论文轨 · 架构书 §六「预注册」 · D5 统计诚实底线）。

预注册是论文轨防「p-hacking」的第一道闸：分析计划在跑数据**之前**登记，
执行时逐口径对账——换方法、换显著性、换 MDE 都会被抓出来。

三条不可让步的规矩：
1. **先登记后执行。** 论文轨执行必须携带已登记的 plan_id，缺失 → PreregistrationRequired
   （该闸门在 plugins/research/prereg_hook.py，本模块只管登记与对账）。
2. **指纹冻结。** plan_fingerprint 对计划做 sort_keys JSON + sha256——
   同一计划指纹稳定，任何字段变更指纹必变（D3：可复现判据）。
3. **台账 append-only。** 同一 plan_id 二次登记拒绝覆盖；历史记录只增不改。
   文件写入走临时文件 + 原子 rename，绝不产生半截记录。

宪法红线：core 不 import plugins（C-1）；路径由调用方注入（C-4）；不 import duckdb（R5）。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

#: 执行时必须与计划逐字一致的口径字段（其余字段属描述性，不参与对账）
COMPARE_FIELDS = ("outcome", "design", "method", "alpha", "mde", "power")

__all__ = [
    "AnalysisPlan", "PlanMismatchError", "PreregLedgerError",
    "plan_fingerprint", "assert_plan_match", "PreRegistrationLedger",
]


class PlanMismatchError(ValueError):
    """执行口径与预注册计划不一致。"""


class PreregLedgerError(ValueError):
    """预注册台账写入被拒（重复 plan_id / 非法记录）。"""


@dataclass(frozen=True)
class AnalysisPlan:
    """一份预注册分析计划（登记即冻结）。"""

    plan_id: str
    hypothesis: str                 # 研究假设（人话）
    outcome: str                    # binary | continuous | count
    design: str                     # rct | observational | two_group
    method: str                     # 登记册方法名（须与 CANDIDATE_FAMILIES 逐字一致）
    alpha: float = 0.05
    mde: float = 0.05
    power: float = 0.8
    primary_metric: str = ""        # 主结果指标名
    epsilon: float | None = None    # 差分隐私预算分配（可选）


def _to_dict(plan: Any) -> dict[str, Any]:
    """dict 直用；AnalysisPlan 转 dict（其余形态直接拒绝，不猜）。"""
    if isinstance(plan, AnalysisPlan):
        return asdict(plan)
    if isinstance(plan, dict):
        return dict(plan)
    raise PreregLedgerError(f"计划必须是 dict 或 AnalysisPlan，实为 {type(plan).__name__}")


def plan_fingerprint(plan: Any) -> str:
    """计划指纹：sort_keys JSON + sha256。

    与执行器 _fingerprint 同一思路——字段顺序无关、可复现、不可逆。
    """
    blob = json.dumps(_to_dict(plan), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_plan_match(plan: Any, executed: dict[str, Any]) -> None:
    """执行口径对账：COMPARE_FIELDS 每项必须与计划逐字一致。

    不一致 → PlanMismatchError，错误信息点名所有差异字段（不只第一个）。
    执行缺字段也算不一致——执行方必须显式给出口径，否则无法证明遵守计划。
    """
    p = _to_dict(plan)
    diffs: list[str] = []
    for field in COMPARE_FIELDS:
        if field not in executed:
            diffs.append(f"{field}(执行缺失)")
            continue
        if str(executed[field]) != str(p.get(field)):
            diffs.append(f"{field}(计划={p.get(field)!r} 执行={executed[field]!r})")
    if diffs:
        raise PlanMismatchError(
            "执行口径与预注册计划不一致：" + "；".join(diffs)
            + "——论文轨不得在执行后改动分析口径（p-hacking 闸门）。"
        )


class PreRegistrationLedger:
    """预注册台账（append-only JSON 文件）。

    存储形态：{"ledger": "preregistration", "version": 1,
               "records": {plan_id: {"registered_at": ..., "plan": {...}}}}
    只增不改：register 对重复 plan_id 抛 PreregLedgerError（R8 纪律同源）。
    写入用临时文件 + 原子 rename，杜绝半截记录。
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    # ---- 内部读写 ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"ledger": "preregistration", "version": 1, "records": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise PreregLedgerError(f"预注册台账损坏：{self.path} -> {e}") from e
        if not isinstance(data.get("records"), dict):
            raise PreregLedgerError(f"预注册台账结构非法：{self.path}")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子替换：读方永远看不到半截文件

    # ---- 对外接口 ----------------------------------------------------------

    def register(self, plan: Any) -> str:
        """登记一份计划。返回 plan_id；重复 plan_id → PreregLedgerError（拒绝覆盖）。"""
        p = _to_dict(plan)
        plan_id = p.get("plan_id")
        if not plan_id:
            raise PreregLedgerError("计划缺少 plan_id——预注册必须可寻址")
        data = self._load()
        records: dict[str, Any] = data["records"]
        if plan_id in records:
            raise PreregLedgerError(
                f"计划 {plan_id} 已登记于 {records[plan_id]['registered_at']}，"
                "append-only 台账拒绝覆盖——如需变更请登记新计划（新 plan_id）。"
            )
        records[plan_id] = {
            "registered_at": datetime.now().isoformat(timespec="seconds"),
            "plan_id": plan_id,
            "fingerprint": plan_fingerprint(p),
            "plan": p,
        }
        self._save(data)
        return plan_id

    def get(self, plan_id: str) -> dict[str, Any] | None:
        """按 plan_id 取回计划（未登记 → None）。"""
        records = self._load()["records"]
        rec = records.get(plan_id)
        return rec["plan"] if rec else None

    def all(self) -> list[dict[str, Any]]:
        """按登记顺序返回全部计划。"""
        return [rec["plan"] for rec in self._load()["records"].values()]

    def recorded_at(self, plan_id: str) -> str | None:
        """计划登记时间（未登记 → None）。"""
        records = self._load()["records"]
        rec = records.get(plan_id)
        return rec["registered_at"] if rec else None

    def new_plan_id(self, prefix: str = "pre") -> str:
        """生成新计划 id（uuid 片段保证唯一）。"""
        return f"{prefix}-{uuid.uuid4().hex[:12]}"
