"""D7 敏感数据分级与动态脱敏。

这是输出边界治理，不改写 raw 批次：分类结果描述字段，脱敏函数返回新对象。
四级从低到高为 ``public``、``internal``、``confidential``、``restricted``。
未知字段默认 internal，宁可收紧也不把未知数据误判为公开。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Iterable, Mapping, Sequence


class SensitivityLevel(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

    @classmethod
    def parse(cls, value: str | int | "SensitivityLevel") -> "SensitivityLevel":
        if isinstance(value, cls):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                return cls(value)
            except ValueError as exc:
                raise SensitivityError(f"非法敏感级别：{value!r}") from exc
        if isinstance(value, str):
            key = value.strip().lower()
            aliases = {
                "l0": cls.PUBLIC, "public": cls.PUBLIC, "公开": cls.PUBLIC,
                "l1": cls.INTERNAL, "internal": cls.INTERNAL, "内部": cls.INTERNAL,
                "l2": cls.CONFIDENTIAL, "confidential": cls.CONFIDENTIAL, "机密": cls.CONFIDENTIAL,
                "l3": cls.RESTRICTED, "restricted": cls.RESTRICTED, "受限": cls.RESTRICTED,
            }
            if key in aliases:
                return aliases[key]
        raise SensitivityError(f"非法敏感级别：{value!r}")

    @property
    def label(self) -> str:
        return ("public", "internal", "confidential", "restricted")[int(self)]


class SensitivityError(ValueError):
    """敏感度策略或脱敏请求不合规。"""


@dataclass(frozen=True)
class FieldClassification:
    field: str
    level: SensitivityLevel
    reason: str
    strategy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "level": self.level.label,
            "reason": self.reason,
            "strategy": self.strategy,
        }


_RESTRICTED = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "access_key",
    "身份证", "证件号", "身份证号", "id_card", "银行卡", "卡号", "bank_account", "bankcard",
)
_CONFIDENTIAL = (
    "email", "e_mail", "邮箱", "phone", "mobile", "手机号", "电话", "姓名", "name",
    "address", "地址", "住址", "生日", "birth", "ip", "device_id", "设备号",
)
_PUBLIC = ("count", "ratio", "rate", "status", "created_at", "date", "日期", "时间")


def _field_key(field: str) -> str:
    if not isinstance(field, str) or not field.strip():
        raise SensitivityError(f"字段名不能为空：{field!r}")
    return field.strip().lower()


def _contains_any(key: str, needles: Iterable[str]) -> bool:
    return any(token in key for token in needles)


def _strategy(key: str, level: SensitivityLevel) -> str:
    if level == SensitivityLevel.RESTRICTED:
        return "redact"
    if _contains_any(key, ("email", "e_mail", "邮箱")):
        return "email"
    if _contains_any(key, ("phone", "mobile", "手机号", "电话")):
        return "phone"
    if _contains_any(key, ("身份证", "证件", "id_card")):
        return "id_card"
    if _contains_any(key, ("name", "姓名")):
        return "name"
    if _contains_any(key, ("address", "地址", "住址")):
        return "address"
    if level >= SensitivityLevel.CONFIDENTIAL:
        return "tokenize"
    return "none"


def classify_field(field: str, declared_level: str | int | SensitivityLevel | None = None) -> FieldClassification:
    """按显式策略优先、字段名启发式其次的方式完成字段分级。"""
    key = _field_key(field)
    if declared_level is not None:
        level = SensitivityLevel.parse(declared_level)
        reason = "显式策略"
    elif _contains_any(key, _RESTRICTED):
        level, reason = SensitivityLevel.RESTRICTED, "命中受限字段规则"
    elif _contains_any(key, _CONFIDENTIAL):
        level, reason = SensitivityLevel.CONFIDENTIAL, "命中机密字段规则"
    elif _contains_any(key, _PUBLIC):
        level, reason = SensitivityLevel.PUBLIC, "命中公开字段规则"
    else:
        level, reason = SensitivityLevel.INTERNAL, "未知字段默认内部级"
    return FieldClassification(field=field, level=level, reason=reason,
                               strategy=_strategy(key, level))


def classify_schema(
    fields: Iterable[str], policy: Mapping[str, str | int | SensitivityLevel] | None = None,
) -> dict[str, FieldClassification]:
    """扫描字段集合并生成可审计分类结果；重复字段和空字段显式拒绝。"""
    result: dict[str, FieldClassification] = {}
    policy = policy or {}
    for field in fields:
        if field in result:
            raise SensitivityError(f"字段重复：{field!r}")
        result[field] = classify_field(field, policy.get(field))
    return result


def load_policy(path: str | Any) -> dict[str, SensitivityLevel]:
    """加载显式字段策略；根节点必须是 ``{"fields": {...}}``。"""
    try:
        raw = json.loads(__import__("pathlib").Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SensitivityError(f"敏感字段策略读取失败：{path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SensitivityError("敏感字段策略 schema_version 必须为 1")
    fields = raw.get("fields")
    if not isinstance(fields, dict):
        raise SensitivityError("敏感字段策略 fields 必须是对象")
    return {str(name): SensitivityLevel.parse(level) for name, level in fields.items()}


def _token(value: Any, salt: str) -> str:
    if not salt:
        raise SensitivityError("tokenize 脱敏必须提供非空 salt")
    digest = hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:12]
    return f"tok_{digest}"


def mask_value(value: Any, classification: FieldClassification, *, reveal_level: SensitivityLevel = SensitivityLevel.INTERNAL,
               salt: str = "traceable-d7") -> Any:
    """按调用方最高可见级别返回脱敏值；不修改输入。"""
    reveal = SensitivityLevel.parse(reveal_level)
    if value is None or classification.level <= reveal:
        return value
    if classification.strategy == "email":
        text = str(value)
        if "@" in text:
            local, domain = text.split("@", 1)
            return f"{(local[:1] if local else '*')}***@{domain}"
    elif classification.strategy == "phone":
        text = str(value)
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 4:
            return "***" + digits[-4:]
    elif classification.strategy == "id_card":
        text = str(value)
        if len(text) >= 4:
            return text[:2] + "*" * (len(text) - 4) + text[-2:]
    elif classification.strategy == "name":
        text = str(value)
        return (text[:1] + "*" * max(1, len(text) - 1)) if text else "*"
    elif classification.strategy == "address":
        text = str(value)
        return text[:2] + "***" if len(text) > 2 else "***"
    if classification.strategy == "tokenize" or classification.level == SensitivityLevel.RESTRICTED:
        return _token(value, salt)
    return "***"


def redact_record(record: Mapping[str, Any], *, reveal_level: SensitivityLevel = SensitivityLevel.INTERNAL,
                  policy: Mapping[str, str | int | SensitivityLevel] | None = None,
                  salt: str = "traceable-d7") -> dict[str, Any]:
    """返回脱敏副本，原 record 保持不变。"""
    classes = classify_schema(record.keys(), policy)
    return {
        field: mask_value(value, classes[field], reveal_level=reveal_level, salt=salt)
        for field, value in record.items()
    }


def redact_rows(rows: Sequence[Mapping[str, Any]], *, reveal_level: SensitivityLevel = SensitivityLevel.INTERNAL,
               policy: Mapping[str, str | int | SensitivityLevel] | None = None,
               salt: str = "traceable-d7") -> list[dict[str, Any]]:
    """批量脱敏；空输入返回空列表，字段集合不一致则拒绝。"""
    if not rows:
        return []
    first_fields = tuple(rows[0].keys())
    out = []
    for row in rows:
        if tuple(row.keys()) != first_fields:
            raise SensitivityError("批量脱敏要求每行字段顺序与集合一致")
        out.append(redact_record(row, reveal_level=reveal_level, policy=policy, salt=salt))
    return out


__all__ = [
    "FieldClassification", "SensitivityError", "SensitivityLevel",
    "classify_field", "classify_schema", "mask_value", "redact_record", "redact_rows",
    "load_policy",
]
