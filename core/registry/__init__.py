"""架构资产注册表（ADR-12）。"""

from .assets import (
    AssetRecord,
    AssetRegistry,
    AssetRegistryError,
    OwnershipQuestions,
    classify_scope,
)

__all__ = [
    "AssetRecord",
    "AssetRegistry",
    "AssetRegistryError",
    "OwnershipQuestions",
    "classify_scope",
]
