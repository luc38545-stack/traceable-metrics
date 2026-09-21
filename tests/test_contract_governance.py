"""D11 契约先行：源 schema 变更与 14 天评审窗口。"""
from pathlib import Path

import pytest

from core.governance.contracts import (
    ColumnSpec,
    ContractChangeError,
    ContractReview,
    SourceContract,
    compare_contracts,
    load_contract_catalog,
    validate_contract_change,
)

ROOT = Path(__file__).resolve().parent.parent


def _contract(version: int, effective: str, *columns: ColumnSpec) -> SourceContract:
    return SourceContract("demo.source", version, effective, tuple(columns))


BASE = _contract(
    1, "2026-01-01T00:00:00Z",
    ColumnSpec("id", "string", False),
    ColumnSpec("value", "number", True),
)


def test_d11_additive_change_is_non_breaking():
    new = _contract(
        2, "2026-01-02T00:00:00Z",
        *BASE.columns, ColumnSpec("label", "string", True),
    )
    changes = validate_contract_change(BASE, new)
    assert [(c.column, c.kind, c.breaking) for c in changes] == [("label", "added", False)]


def test_d11_removed_column_requires_review():
    new = _contract(2, "2026-02-01T00:00:00Z", BASE.columns[0])
    with pytest.raises(ContractChangeError, match="必须先提交评审"):
        validate_contract_change(BASE, new)


def test_d11_type_change_is_breaking():
    new = _contract(
        2, "2026-02-01T00:00:00Z",
        BASE.columns[0], ColumnSpec("value", "integer", True),
    )
    changes = compare_contracts(BASE, new)
    assert changes[0].kind == "type_changed" and changes[0].breaking


def test_d11_review_must_wait_14_days():
    new = _contract(2, "2026-02-14T00:00:00Z", BASE.columns[0])
    review = ContractReview("rev-1", "alice", "2026-02-01T00:00:00Z", new.effective_at)
    with pytest.raises(ContractChangeError, match="至少 14 天"):
        validate_contract_change(BASE, new, review)


def test_d11_approved_breaking_change_after_notice_passes():
    new = _contract(2, "2026-02-15T00:00:00Z", BASE.columns[0])
    review = ContractReview("rev-2", "alice", "2026-02-01T00:00:00Z", new.effective_at)
    changes = validate_contract_change(BASE, new, review, now="2026-02-02T00:00:00Z")
    assert changes[0].kind == "removed"


def test_d11_review_and_effective_time_must_match():
    new = _contract(2, "2026-02-15T00:00:00Z", BASE.columns[0])
    review = ContractReview("rev-3", "alice", "2026-02-01T00:00:00Z", "2026-02-16T00:00:00Z")
    with pytest.raises(ContractChangeError, match="effective_at"):
        validate_contract_change(BASE, new, review)


def test_d11_seed_catalog_loads():
    catalog = load_contract_catalog(ROOT / "infra" / "contracts" / "source_catalog.json")
    assert set(catalog) == {"commerce.orders", "research.cohort"}
    assert catalog["research.cohort"].columns[0].name == "participant_id"


def test_d11_invalid_column_type_rejected():
    with pytest.raises(ContractChangeError, match="列类型不支持"):
        ColumnSpec("id", "json").validate()
