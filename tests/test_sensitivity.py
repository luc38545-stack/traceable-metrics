"""D7 四级敏感数据分类与动态脱敏验收。"""
import pytest

from core.governance.sensitivity import (
    SensitivityError,
    SensitivityLevel,
    classify_field,
    classify_schema,
    load_policy,
    redact_record,
    redact_rows,
)


def test_d7_four_levels_and_safe_unknown_default():
    assert classify_field("order_count").level == SensitivityLevel.PUBLIC
    assert classify_field("team_code").level == SensitivityLevel.INTERNAL
    assert classify_field("email").level == SensitivityLevel.CONFIDENTIAL
    assert classify_field("api_token").level == SensitivityLevel.RESTRICTED


def test_d7_explicit_policy_overrides_heuristic():
    out = classify_schema(["email", "note"], {"email": "restricted", "note": "public"})
    assert out["email"].level == SensitivityLevel.RESTRICTED
    assert out["note"].level == SensitivityLevel.PUBLIC
    assert out["email"].reason == "显式策略"


def test_d7_public_boundary_masks_internal_and_above():
    row = {"order_count": 3, "team_code": "C-1", "email": "alice@example.com", "api_token": "sk-secret"}
    masked = redact_record(row, reveal_level=SensitivityLevel.PUBLIC)
    assert masked["order_count"] == 3
    assert masked["team_code"] == "***"
    assert masked["email"] == "a***@example.com"
    assert masked["api_token"].startswith("tok_")
    assert row["api_token"] == "sk-secret"


def test_d7_internal_boundary_keeps_internal_masks_restricted():
    row = {"team_code": "C-1", "phone": "13812345678", "password": "secret"}
    masked = redact_record(row)
    assert masked["team_code"] == "C-1"
    assert masked["phone"] == "***5678"
    assert masked["password"].startswith("tok_")


def test_d7_confidential_reveal_requires_explicit_higher_level():
    row = {"email": "alice@example.com", "id_card": "110101199001011234"}
    masked = redact_record(row, reveal_level=SensitivityLevel.CONFIDENTIAL)
    assert masked["email"] == row["email"]
    assert masked["id_card"] != row["id_card"], "受限级字段不能由机密级权限解锁"
    restricted = redact_record(row, reveal_level=SensitivityLevel.INTERNAL)
    assert restricted["email"] != row["email"]
    assert restricted["id_card"] != row["id_card"]


def test_d7_batch_rejects_mismatched_shapes():
    with pytest.raises(SensitivityError, match="字段顺序与集合一致"):
        redact_rows([{"email": "a@x"}, {"phone": "1"}])


def test_d7_invalid_policy_and_duplicate_field_rejected():
    with pytest.raises(SensitivityError, match="非法敏感级别"):
        classify_field("email", "secret-level")
    with pytest.raises(SensitivityError, match="字段重复"):
        classify_schema(["email", "email"])


def test_d7_tokenization_requires_salt():
    with pytest.raises(SensitivityError, match="salt"):
        redact_record({"password": "secret"}, salt="")


def test_d7_policy_file_is_loadable():
    policy = load_policy(__import__("pathlib").Path("infra/governance/sensitivity_policy.json"))
    assert policy["email"] == SensitivityLevel.CONFIDENTIAL
    assert policy["id_card"] == SensitivityLevel.RESTRICTED
