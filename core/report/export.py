"""报告导出器（共享核心 · 架构书 §04 L4「导出与水印」）。

三件事，一件都不能少：
- 文件名带轨道前缀 [C]/[R]（ADR-14 stamping · 文件级）
- 落盘后算 sha256，与 run_id 一并进审计流（D8 审计全覆盖）
- 导出目录由调用方注入；core 不选择数据落在哪（C-4）

导出物是「可发给别人看」的东西，所以它必须自带溯源页脚——水印由 kernel 注入。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from core.audit import ledger
from core.report.kernel import ReportBlocked, build_conclusion, render_html, track_band


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def export_report(
    ledger_json: dict,
    claims_result: dict,
    out_dir: Path,
    ledger_db: Path,
    health_issues: list[dict] | None = None,
    llm_explanation: dict | None = None,
    business_context: dict | None = None,
) -> dict:
    """渲染并落盘一份 HTML 报告，返回导出元数据。

    claims 对账不通过 → ReportBlocked，不落任何文件（ADR-18：FAIL 即拦截）。
    llm_explanation（可选）透传给渲染内核（V1-DoD「LLM 解释(claims 过闸)→导出」），
    未配置/未过闸由内核显性标注，不伪装成品（P4）。
    out_dir / ledger_db 均由调用方注入。
    """
    conclusion = build_conclusion(
        ledger_json, claims_result, health_issues,
        business_context=business_context,
    )
    html = render_html(conclusion, llm_explanation=llm_explanation)

    band = track_band(conclusion["track"])
    run_id = ledger_json.get("run_id", "unknown")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    display_name = str(
        (conclusion.get("business_context") or {}).get("display_name")
        or conclusion["metric"]["name"]
    )
    safe_name = "".join(ch for ch in display_name if ch.isalnum() or ch in "-_")[:40]
    name = f"{band['prefix']}TraceableMetrics报告_{safe_name or conclusion['metric']['name']}_{stamp}.html"

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / name
    dest.write_text(html, encoding="utf-8")

    export_id = f"e-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    sha = _sha256(dest)
    ledger.log_export(
        ledger_db, export_id=export_id, run_id=run_id, track=conclusion["track"],
        metric=conclusion["metric"]["name"], file_name=name, dest=str(dest), sha256=sha,
    )
    return {
        "export_id": export_id,
        "file_name": name,
        "path": str(dest),
        "sha256": sha,
        "track": conclusion["track"],
        "conclusion_id": conclusion["conclusion_id"],
    }


__all__ = ["export_report", "build_conclusion", "render_html", "ReportBlocked"]
