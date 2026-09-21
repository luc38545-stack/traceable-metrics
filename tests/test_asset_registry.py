"""ADR-12 资产注册表与 P8' 三问验收。"""
from pathlib import Path

import pytest

from core.registry.assets import (
    AssetRecord,
    AssetRegistry,
    AssetRegistryError,
    OwnershipQuestions,
    classify_scope,
)

ROOT = Path(__file__).resolve().parent.parent


def _q(**overrides: bool) -> OwnershipQuestions:
    values = {
        "depends_on_track_semantics": False,
        "failure_contaminates_other_track": False,
        "managed_data_flow": False,
    }
    values.update(overrides)
    return OwnershipQuestions(**values)


def _record(asset_id: str = "demo.shared", scope: str = "shared",
            questions: OwnershipQuestions | None = None,
            rationale: str = "测试资产归属") -> AssetRecord:
    return AssetRecord(
        asset_id=asset_id,
        path="core/demo.py",
        kind="module",
        scope=scope,
        rationale=rationale,
        questions=questions or _q(),
    )


def test_adr12_shared_scope_from_three_no_answers():
    assert classify_scope(_q()) == "shared"


def test_adr12_track_scope_requires_explicit_track():
    with pytest.raises(AssetRegistryError, match="显式指定"):
        classify_scope(_q(managed_data_flow=True))
    assert classify_scope(_q(managed_data_flow=True), "research") == "research"


def test_adr12_scope_mismatch_rejected():
    with pytest.raises(AssetRegistryError, match="scope 与 P8"):
        _record(scope="shared", questions=_q(depends_on_track_semantics=True)).validate()


def test_adr12_duplicate_registration_rejected():
    registry = AssetRegistry([_record()])
    with pytest.raises(AssetRegistryError, match="不允许覆盖"):
        registry.register(_record())


def test_adr12_path_traversal_rejected():
    with pytest.raises(AssetRegistryError, match="相对路径"):
        AssetRecord(
            asset_id="bad.path", path="../secret.py", kind="module", scope="shared",
            rationale="越界", questions=_q(),
        ).validate()


def test_adr12_seed_registry_loads_and_has_all_scopes():
    registry = AssetRegistry.load(ROOT / "infra" / "registry" / "assets.json")
    assert len(registry.records()) >= 6
    assert {r.scope for r in registry.records()} == {"shared", "commerce", "research"}
    assert registry.get("core.experiment.hashing").scope == "shared"


def test_adr12_registry_is_append_only_on_disk(tmp_path: Path):
    path = tmp_path / "assets.json"
    registry = AssetRegistry([_record()])
    registry.save(path)
    registry.register(_record("demo.research", "research", _q(managed_data_flow=True)))
    registry.save(path)
    loaded = AssetRegistry.load(path)
    assert {r.asset_id for r in loaded.records()} == {"demo.shared", "demo.research"}

    changed = AssetRegistry([_record("demo.shared", rationale="篡改历史")])
    with pytest.raises(AssetRegistryError, match="修改/删除历史"):
        changed.save(path)
