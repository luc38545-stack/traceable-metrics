"""差分隐私预算账本（S3 论文轨 · 架构书 §六「差分隐私预算」· D8 审计全覆盖）。

论文轨多次发布统计量时，每次披露都在消耗 ε 预算。本账本把「花过多少 ε、
还剩多少」变成可机查的 append-only 记录——与运行台账同一纪律（只增不改）。

规则：
1. **超预算拒绝。** 本次支出会让累计 spent > total_epsilon → PrivacyBudgetExhausted，
   且被拒支出**绝不入账**（不产生半途记录）。
2. **append-only。** 同一 spend_id 重复支出 → DpLedgerError（拒绝覆盖历史）。
3. **原子写。** 临时文件 + os.replace，读方永远看不到半截账本。
4. **核心不选存储。** 文件路径由调用方注入（C-4）；不 import duckdb（R5）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["DpBudgetLedger", "PrivacyBudgetExhausted", "DpLedgerError"]

_EPS_TOL = 1e-9


class PrivacyBudgetExhausted(RuntimeError):
    """DP 预算已耗尽：本次支出会超出总预算。"""


class DpLedgerError(ValueError):
    """DP 账本写入被拒（重复 spend_id / 非法账本）。"""


class DpBudgetLedger:
    """差分隐私预算账本（append-only JSON）。

    存储形态：{"ledger": "dp_budget", "version": 1, "total_epsilon": 1.0,
               "spends": {spend_id: {"epsilon": ..., "purpose": ..., "spent_at": ...}}}
    """

    def __init__(self, path: Path, total_epsilon: float = 1.0):
        self.path = Path(path)
        self.total_epsilon = float(total_epsilon)
        if self.total_epsilon <= 0:
            raise DpLedgerError(f"总预算必须为正，实为 {self.total_epsilon}")

    # ---- 内部读写 ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"ledger": "dp_budget", "version": 1,
                    "total_epsilon": self.total_epsilon, "spends": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise DpLedgerError(f"DP 预算账本损坏：{self.path} -> {e}") from e
        if not isinstance(data.get("spends"), dict):
            raise DpLedgerError(f"DP 预算账本结构非法：{self.path}")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- 对外接口 ----------------------------------------------------------

    def spend(self, spend_id: str, epsilon: float, purpose: str = "") -> float:
        """记一笔 ε 支出。返回最新结余；超预算/重复 id → 拒绝且不入账。"""
        eps = float(epsilon)
        if eps < 0:
            raise DpLedgerError(f"支出不能为负，实为 {eps}")
        data = self._load()
        spends: dict[str, Any] = data["spends"]
        if spend_id in spends:
            raise DpLedgerError(
                f"spend {spend_id!r} 已入账（{spends[spend_id]['spent_at']}），"
                "append-only 账本拒绝覆盖——新披露请用新 spend_id。"
            )
        spent_so_far = sum(float(s["epsilon"]) for s in spends.values())
        if spent_so_far + eps > self.total_epsilon + _EPS_TOL:
            raise PrivacyBudgetExhausted(
                f"DP 预算耗尽：已花 {spent_so_far:.4f}/{self.total_epsilon}，"
                f"本次 {eps:.4f} 将超出总预算——本次披露被拒，未入账。"
            )
        spends[spend_id] = {
            "epsilon": eps,
            "purpose": purpose,
            "spent_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save(data)
        return self.total_epsilon - spent_so_far - eps

    def spent(self) -> float:
        """累计已花 ε。"""
        spends = self._load()["spends"]
        return sum(float(s["epsilon"]) for s in spends.values())

    def remaining(self) -> float:
        """剩余预算 = 总预算 - 已花。"""
        return self.total_epsilon - self.spent()

    def entries(self) -> list[dict[str, Any]]:
        """全部支出记录（按 spend_id 字典序）。"""
        spends = self._load()["spends"]
        return [{"spend_id": sid, **s} for sid, s in sorted(spends.items())]
