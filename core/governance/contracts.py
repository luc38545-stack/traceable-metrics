"""D11 契约先行：源 schema 变更比较、评审与 14 天预告门禁。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_TYPES = frozenset({"string", "integer", "number", "boolean", "date", "datetime"})


class ContractChangeError(ValueError):
    """源契约或变更评审不合规。"""


def _utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractChangeError(f"{field} 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ContractChangeError(f"{field} 必须包含时区，统一使用 UTC")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    nullable: bool = True

    def validate(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ContractChangeError(f"列名非法：{self.name!r}")
        if self.type not in _TYPES:
            raise ContractChangeError(f"列类型不支持：{self.type!r}")
        if type(self.nullable) is not bool:
            raise ContractChangeError(f"nullable 必须是 bool：{self.name}")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ColumnSpec":
        try:
            value = cls(str(raw["name"]), str(raw["type"]), raw.get("nullable", True))
        except (KeyError, TypeError) as exc:
            raise ContractChangeError(f"列契约字段不完整：{exc}") from exc
        value.validate()
        return value


@dataclass(frozen=True)
class SourceContract:
    source: str
    version: int
    effective_at: str
    columns: tuple[ColumnSpec, ...]

    def validate(self) -> None:
        if not self.source.strip():
            raise ContractChangeError("source 不能为空")
        if type(self.version) is not int or self.version < 1:
            raise ContractChangeError("契约 version 必须是正整数")
        _utc(self.effective_at, "effective_at")
        names: set[str] = set()
        for column in self.columns:
            column.validate()
            if column.name in names:
                raise ContractChangeError(f"契约列重复：{column.name}")
            names.add(column.name)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceContract":
        try:
            columns = tuple(ColumnSpec.from_dict(item) for item in raw["columns"])
            value = cls(str(raw["source"]), raw["version"], str(raw["effective_at"]), columns)
        except (KeyError, TypeError) as exc:
            raise ContractChangeError(f"源契约字段不完整：{exc}") from exc
        value.validate()
        return value


@dataclass(frozen=True)
class ContractChange:
    column: str
    kind: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    @property
    def breaking(self) -> bool:
        return self.kind in {"removed", "type_changed", "nullability_tightened"}


@dataclass(frozen=True)
class ContractReview:
    review_id: str
    reviewer: str
    reviewed_at: str
    effective_at: str
    decision: str = "approved"

    def validate(self) -> None:
        if not self.review_id.strip() or not self.reviewer.strip():
            raise ContractChangeError("破坏性变更评审必须有 review_id 和 reviewer")
        if self.decision != "approved":
            raise ContractChangeError(f"契约变更评审未批准：{self.decision!r}")
        reviewed = _utc(self.reviewed_at, "reviewed_at")
        effective = _utc(self.effective_at, "effective_at")
        if effective < reviewed + timedelta(days=14):
            raise ContractChangeError("破坏性契约变更生效时间必须晚于评审至少 14 天")


def compare_contracts(old: SourceContract, new: SourceContract) -> tuple[ContractChange, ...]:
    """比较两个版本，明确列出新增、删除、类型和可空性变化。"""
    old.validate()
    new.validate()
    if old.source != new.source:
        raise ContractChangeError("只能比较同一个 source 的契约")
    before = {c.name: c for c in old.columns}
    after = {c.name: c for c in new.columns}
    changes: list[ContractChange] = []
    for name in sorted(before.keys() - after.keys()):
        c = before[name]
        changes.append(ContractChange(name, "removed", {"type": c.type, "nullable": c.nullable}))
    for name in sorted(after.keys() - before.keys()):
        c = after[name]
        changes.append(ContractChange(name, "added", None, {"type": c.type, "nullable": c.nullable}))
    for name in sorted(before.keys() & after.keys()):
        old_col, new_col = before[name], after[name]
        if old_col.type != new_col.type:
            changes.append(ContractChange(
                name, "type_changed", {"type": old_col.type, "nullable": old_col.nullable},
                {"type": new_col.type, "nullable": new_col.nullable},
            ))
        elif old_col.nullable and not new_col.nullable:
            changes.append(ContractChange(
                name, "nullability_tightened", {"nullable": True}, {"nullable": False},
            ))
    return tuple(changes)


def validate_contract_change(
    old: SourceContract,
    new: SourceContract,
    review: ContractReview | None = None,
    *,
    now: str | None = None,
) -> tuple[ContractChange, ...]:
    """执行 D11 门禁；非破坏性新增可直接通过，破坏性变化必须延迟 14 天。"""
    changes = compare_contracts(old, new)
    if new.version <= old.version:
        raise ContractChangeError("新契约 version 必须递增")
    breaking = tuple(change for change in changes if change.breaking)
    if not breaking:
        return changes
    if review is None:
        raise ContractChangeError("检测到破坏性契约变更，必须先提交评审")
    review.validate()
    effective = _utc(new.effective_at, "new.effective_at")
    if effective != _utc(review.effective_at, "review.effective_at"):
        raise ContractChangeError("契约 effective_at 必须与评审记录一致")
    if now is not None and effective <= _utc(now, "now"):
        raise ContractChangeError("破坏性契约变更的生效时间必须晚于当前时间")
    return changes


def load_contract_catalog(path: Path) -> dict[str, SourceContract]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractChangeError(f"源契约目录读取失败：{path}") from exc
    if raw.get("schema_version") != 1 or not isinstance(raw.get("contracts"), list):
        raise ContractChangeError("源契约目录 schema_version 或 contracts 非法")
    out: dict[str, SourceContract] = {}
    for item in raw["contracts"]:
        contract = SourceContract.from_dict(item)
        if contract.source in out:
            raise ContractChangeError(f"源契约重复：{contract.source}")
        out[contract.source] = contract
    return out


__all__ = [
    "ColumnSpec", "ContractChange", "ContractChangeError", "ContractReview",
    "SourceContract", "compare_contracts", "load_contract_catalog", "validate_contract_change",
]
