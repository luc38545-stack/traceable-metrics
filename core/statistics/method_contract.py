"""统计方法契约（附录 T 冻结项⑧ · §04-L3）。

架构书要求：
- 候选族登记册：数据类型(outcome) × 研究设计(design) → 方法集
- 比例差异 → two-proportion test / logistic / Fisher（**不再产出 welch_t**，回归用例）
- 三色判定状态机：GREEN=前提满足 / YELLOW=轻度违反（必出未检项清单+敏感性检验）/
  RED=方法不适用（拒出 p 值，只出诊断书）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 候选族登记册（§04-L3 方法契约） ----
OUTCOME_BINARY = "binary"
OUTCOME_CONTINUOUS = "continuous"
OUTCOME_COUNT = "count"

DESIGN_TWO_GROUP = "two_group"
DESIGN_RCT = "rct"
DESIGN_OBSERVATIONAL = "observational"

CANDIDATE_FAMILIES: dict[tuple[str, str], list[str]] = {
    ("binary", "two_group"): ["two_proportion_test", "logistic", "fisher"],
    ("binary", "rct"): ["two_proportion_test", "logistic"],
    ("binary", "observational"): ["logistic", "propensity_score"],
    ("continuous", "two_group"): ["welch_t_test", "mann_whitney"],
    ("continuous", "rct"): ["welch_t_test", "ancova"],
    ("continuous", "observational"): ["ols_regression", "matching"],
    ("count", "two_group"): ["poisson_test", "negative_binomial"],
    ("count", "rct"): ["poisson_test"],
    ("count", "observational"): ["poisson_regression", "negative_binomial"],
}


class MethodContractError(ValueError):
    """方法契约不合法。"""


class FrozenRegistryError(MethodContractError):
    """方法登记册已冻结——拒绝任何改动（架构书 §六 S3「登记册 + 冻结开关」）。"""


class MethodRegistry:
    """research-methods 登记册（可冻结）。

    冻结语义（实现取舍，如实标注在审计报告）：
    - ``freeze()`` 之后 ``register_family`` 一律抛 ``FrozenRegistryError``；
    - 冻结**不可逆**（不提供 unfreeze）——需要不同方法集请新建实例；
      「冻结」二字本义如此，也杜绝「run 期间偷偷解冻改方法」这种事后合理化；
    - 取候选族永远返回**副本**：外部改动返回值不得污染登记册，否则冻结形同虚设。
    """

    def __init__(self, table: dict[tuple[str, str], list[str]] | None = None,
                 *, frozen: bool = False) -> None:
        src = table if table is not None else CANDIDATE_FAMILIES
        self._table: dict[tuple[str, str], list[str]] = {
            k: list(v) for k, v in src.items()}
        self._frozen = bool(frozen)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        """冻结登记册（不可逆）。"""
        self._frozen = True

    def register_family(
        self, outcome: str, design: str, methods: list[str], *, replace: bool = False
    ) -> None:
        """登记/覆盖一个 outcome×design 的候选族。冻结后一律拒绝。"""
        if self._frozen:
            raise FrozenRegistryError(
                f"方法登记册已冻结，拒绝改动 {outcome}×{design}；"
                "如需变更方法集请新建登记册实例（冻结不可逆，见架构书 §六 S3）。"
            )
        key = (outcome, design)
        if key in self._table and not replace:
            raise MethodContractError(
                f"{outcome}×{design} 已登记于冻结登记册，覆盖需显式 replace=True")
        if not methods:
            raise MethodContractError("候选族不得为空")
        self._table[key] = list(methods)

    def families(self, outcome: str, design: str) -> list[str]:
        """取候选族（副本）。未知组合 → 拒绝（不静默）。"""
        key = (outcome, design)
        if key not in self._table:
            raise MethodContractError(f"unknown outcome×design: {key}")
        return list(self._table[key])

    def snapshot(self) -> dict[str, list[str]]:
        """登记册快照（可序列化，供台账溯源与复现包核验）。"""
        return {f"{o}×{d}": list(v) for (o, d), v in sorted(self._table.items())}

    def fingerprint(self) -> str:
        """登记册内容指纹：方法集一变，指纹必变（防「边跑边改方法」）。"""
        import hashlib
        import json as _json

        blob = _json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


#: 默认登记册（模块级；需要隔离/冻结场景请用 MethodRegistry 新建实例）
DEFAULT_REGISTRY = MethodRegistry()


def candidate_families(
    outcome: str, design: str, registry: MethodRegistry | None = None
) -> list[str]:
    """从登记册取候选族（副本）。未知组合 → 拒绝（不静默）。

    registry 省略时用默认登记册（向后兼容两参数调用）。
    """
    return (registry or DEFAULT_REGISTRY).families(outcome, design)


def register_family(
    outcome: str,
    design: str,
    methods: list[str],
    *,
    registry: MethodRegistry | None = None,
    replace: bool = False,
) -> None:
    """登记/覆盖候选族（冻结后拒绝）。"""
    (registry or DEFAULT_REGISTRY).register_family(
        outcome, design, methods, replace=replace)


def registry_fingerprint(registry: MethodRegistry | None = None) -> str:
    """登记册内容指纹（sha256 hex）。"""
    return (registry or DEFAULT_REGISTRY).fingerprint()


# ---- 三色判定状态机 ----
GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"


@dataclass
class CheckResult:
    """一次前提检查的结论。"""

    status: str                      # GREEN / YELLOW / RED
    unchecked_items: list[str] = field(default_factory=list)   # YELLOW 必填
    sensitivity_required: bool = False                          # YELLOW 必填
    diagnosis: str | None = None                                # RED 时必填（人话诊断书）

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "unchecked_items": self.unchecked_items,
            "sensitivity_required": self.sensitivity_required,
            "diagnosis": self.diagnosis,
        }


def three_color(checks: dict[str, bool]) -> CheckResult:
    """把逐项前提检查结果折叠为三色判定。

    checks: {检查名: 是否通过}；未出现的检查项 → 计入 YELLOW 的未检项清单。
    """
    passed = [k for k, v in checks.items() if v]
    failed = [k for k, v in checks.items() if not v]

    if failed:
        # RED：方法不适用 → 拒出 p 值，只出诊断书（D5）
        return CheckResult(
            status=RED,
            unchecked_items=list(failed),
            diagnosis=(
                "前提检查未通过：" + "、".join(failed)
                + "。本方法不适用，建议改用候选族中的替代方法（见诊断书）。"
            ),
        )
    if not checks:
        # 一个检查都没跑 = 未知前提状态 → 保守 YELLOW
        return CheckResult(
            status=YELLOW,
            unchecked_items=["(未执行任何前提检查)"],
            sensitivity_required=True,
        )
    return CheckResult(status=GREEN)
