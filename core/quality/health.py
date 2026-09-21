"""数据体检引擎（共享核心）—— 非程序员主旅程的第一站。

产出人话问题清单 + 修复建议；修复 = 新建清洗批次，绝不改动 raw（P3）。
存储访问一律经 core.storage.db 抽象接口（宪法 C-3：core 不硬编码存储引擎）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.storage import db as storage


@dataclass
class HealthIssue:
    code: str            # e.g. "blank_amount" | "date_format_mixed"
    severity: str        # "warn" | "error"
    human_text: str      # 人话描述（Grandma 测试通过）
    fix_suggestion: str
    impact: str
    affected_rows: int = 0
    accepted_fix: bool = False
    column: str | None = None


@dataclass
class HealthReport:
    source: Path
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    issues: list[HealthIssue] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "issue_count": len(self.issues),
            "issues": [i.code for i in self.issues],
            "issue_details": [{
                "code": i.code,
                "severity": i.severity,
                "column": i.column,
                "human_text": i.human_text,
                "impact": i.impact,
                "fix_suggestion": i.fix_suggestion,
                "affected_rows": i.affected_rows,
            } for i in self.issues],
        }


def _col_matches(col: str, keywords: tuple[str, ...]) -> bool:
    return any(k in col.lower() for k in keywords)


_DATE_KW = ("date", "日期", "时间", "time")
_AMOUNT_KW = ("amount", "金额", "price", "数量", "qty")


def run_health_check(csv_path: Path) -> HealthReport:
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    report = HealthReport(source=csv_path)
    conn = storage.connect()  # 内存库；存储实现经抽象接口注入
    try:
        rel = storage.read_csv(conn, csv_path)
        cols = [c for c in rel.columns]
        report.columns = cols
        report.rows = rel.count("*").fetchone()[0]

        # 1) 日期格式混用检查：同一列内同时存在 "2026-08-26" 与 "2026/08/26" 两类形态
        for col in cols:
            if not _col_matches(col, _DATE_KW):
                continue
            try:
                n_slash = rel.filter(
                    f'"{col}" IS NOT NULL AND CAST("{col}" AS VARCHAR) LIKE \'%/%\''
                ).count("*").fetchone()[0]
                n_dash = rel.filter(
                    f'"{col}" IS NOT NULL AND CAST("{col}" AS VARCHAR) LIKE \'%-%\''
                ).count("*").fetchone()[0]
            except Exception:  # 类型不可转字符串（如纯数值列）则跳过
                continue
            if n_slash and n_dash:
                report.issues.append(
                    HealthIssue(
                        code="date_format_mixed",
                        severity="warn",
                        human_text=f"「{col}」列混用了两种日期写法（如 2026-08-26 和 2026/08/26）",
                        fix_suggestion="统一为 2026-08-26 格式（ISO）",
                        impact="影响按日期的汇总与对比",
                        affected_rows=n_slash + n_dash,
                        column=col,
                    )
                )

        # 2) 金额/数量类空值检查
        for col in cols:
            if not _col_matches(col, _AMOUNT_KW):
                continue
            try:
                n_blank = rel.filter(
                    f'"{col}" IS NULL OR TRIM(CAST("{col}" AS VARCHAR)) = \'\''
                ).count("*").fetchone()[0]
            except Exception:
                n_blank = 0
            if n_blank:
                report.issues.append(
                    HealthIssue(
                        code="blank_value",
                        severity="warn",
                        human_text=f"「{col}」列有 {n_blank} 行空白",
                        fix_suggestion="标记为缺失并按业务规则处理（如取消单不计入金额统计）",
                        impact="不处理会导致金额类汇总偏低",
                        affected_rows=n_blank,
                        column=col,
                    )
                )
    finally:
        conn.close()
    return report
