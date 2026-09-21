"""ADR-12 资产注册表与 P8' 归属判定。

注册表是治理元数据，不承载业务数据。每项新增能力必须回答 P8' 三问，
并把判定后的 ``scope`` 写入不可删除、不可改写历史记录的注册表。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

SCOPES = frozenset({"shared", "commerce", "research"})
_ASSET_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class AssetRegistryError(ValueError):
    """资产注册表拒绝不合规记录。"""


@dataclass(frozen=True)
class OwnershipQuestions:
    """P8' 三问：轨道语义、对侧污染、受管数据流动。"""

    depends_on_track_semantics: bool
    failure_contaminates_other_track: bool
    managed_data_flow: bool

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not bool:
                raise AssetRegistryError(f"P8' 三问必须是 bool：{name}")

    def to_dict(self) -> dict[str, bool]:
        self.validate()
        return asdict(self)


def classify_scope(questions: OwnershipQuestions, track: str | None = None) -> str:
    """依据 P8' 三问判定资产 scope。

    三问全为否时只能归 ``shared``；只要任一回答为是，就必须显式给出
    ``commerce`` 或 ``research``，避免系统猜测轨道归属。
    """
    questions.validate()
    if track is not None and track not in SCOPES - {"shared"}:
        raise AssetRegistryError(f"非法轨道 scope：{track!r}")
    track_specific = any(asdict(questions).values())
    if not track_specific:
        if track is not None:
            raise AssetRegistryError("P8' 三问全为否的资产必须登记为 shared")
        return "shared"
    if track is None:
        raise AssetRegistryError("P8' 三问出现是时必须显式指定 commerce 或 research")
    return track


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    path: str
    kind: str
    scope: str
    rationale: str
    questions: OwnershipQuestions

    def validate(self) -> None:
        if not _ASSET_ID.fullmatch(self.asset_id):
            raise AssetRegistryError(f"asset_id 格式非法：{self.asset_id!r}")
        if not self.path or Path(self.path).is_absolute() or ".." in Path(self.path).parts:
            raise AssetRegistryError(f"资产路径必须是仓库内相对路径：{self.path!r}")
        if not self.kind.strip():
            raise AssetRegistryError("资产 kind 不能为空")
        if self.scope not in SCOPES:
            raise AssetRegistryError(f"非法资产 scope：{self.scope!r}")
        if not self.rationale.strip():
            raise AssetRegistryError("资产 rationale 不能为空")
        if self.scope == "shared" and any(asdict(self.questions).values()):
            raise AssetRegistryError("scope 与 P8' 三问不一致：shared 记录含有是")
        expected = classify_scope(self.questions, self.scope if self.scope != "shared" else None)
        if expected != self.scope:
            raise AssetRegistryError(
                f"scope 与 P8' 三问不一致：声明 {self.scope!r}，判定 {expected!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "asset_id": self.asset_id,
            "path": self.path,
            "kind": self.kind,
            "scope": self.scope,
            "rationale": self.rationale,
            "questions": self.questions.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AssetRecord":
        try:
            q = raw["questions"]
            record = cls(
                asset_id=str(raw["asset_id"]),
                path=str(raw["path"]),
                kind=str(raw["kind"]),
                scope=str(raw["scope"]),
                rationale=str(raw["rationale"]),
                questions=OwnershipQuestions(
                    depends_on_track_semantics=q["depends_on_track_semantics"],
                    failure_contaminates_other_track=q["failure_contaminates_other_track"],
                    managed_data_flow=q["managed_data_flow"],
                ),
            )
        except (KeyError, TypeError) as exc:
            raise AssetRegistryError(f"资产记录字段不完整：{exc}") from exc
        record.validate()
        return record


class AssetRegistry:
    """内存注册表；register 只允许新增，禁止覆盖或删除已有资产。"""

    SCHEMA_VERSION = 1

    def __init__(self, records: Iterable[AssetRecord] = ()) -> None:
        self._records: dict[str, AssetRecord] = {}
        for record in records:
            self.register(record)

    def register(self, record: AssetRecord) -> None:
        record.validate()
        if record.asset_id in self._records:
            raise AssetRegistryError(
                f"资产已登记，注册表只允许追加，不允许覆盖：{record.asset_id}"
            )
        self._records[record.asset_id] = record

    def get(self, asset_id: str) -> AssetRecord:
        try:
            return self._records[asset_id]
        except KeyError as exc:
            raise AssetRegistryError(f"资产未登记：{asset_id}") from exc

    def records(self) -> tuple[AssetRecord, ...]:
        return tuple(self._records[k] for k in sorted(self._records))

    def by_scope(self, scope: str) -> tuple[AssetRecord, ...]:
        if scope not in SCOPES:
            raise AssetRegistryError(f"非法资产 scope：{scope!r}")
        return tuple(r for r in self.records() if r.scope == scope)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "assets": [r.to_dict() for r in self.records()],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AssetRegistry":
        if raw.get("schema_version") != cls.SCHEMA_VERSION:
            raise AssetRegistryError("不支持的资产注册表 schema_version")
        assets = raw.get("assets")
        if not isinstance(assets, list):
            raise AssetRegistryError("资产注册表 assets 必须是数组")
        return cls(AssetRecord.from_dict(item) for item in assets)

    @classmethod
    def load(cls, path: Path) -> "AssetRegistry":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssetRegistryError(f"资产注册表读取失败：{path}") from exc
        if not isinstance(raw, dict):
            raise AssetRegistryError("资产注册表根节点必须是对象")
        return cls.from_dict(raw)

    def save(self, path: Path) -> None:
        """原子写入，并拒绝覆盖已有记录或删除历史记录。"""
        path = Path(path)
        if path.exists():
            existing = self.load(path)
            for old in existing.records():
                if old.asset_id not in self._records or self._records[old.asset_id] != old:
                    raise AssetRegistryError(
                        f"注册表只允许追加，不能修改/删除历史资产：{old.asset_id}"
                    )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)


__all__ = [
    "AssetRecord",
    "AssetRegistry",
    "AssetRegistryError",
    "OwnershipQuestions",
    "SCOPES",
    "classify_scope",
]
