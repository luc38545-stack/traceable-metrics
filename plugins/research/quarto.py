"""Quarto 出版（S3 论文轨 · 架构书 §六「Quarto 出版」· D8 审计全覆盖）。

论文轨的发表物是 Quarto 文档（.qmd）——不是一次性 HTML，而是
「文字 + 代码 + 数据 + 结论」一体的可复现论文。

规则：
- **[R] 前缀 + 结论数字走插值**：文件名 [R]…，正文数字一律来自 conclusion
  对象（{effect_value} / {p_value} 占位符 + YAML params 承载实际值），
  模板本身不写死任何数字（R7 精神，插件侧同样遵守）；
- **发布必须进审计**：publish_quarto 通过 ledger.log_export 记录
  （D8：导出/分享动作必须可追溯，带 sha256）；
- 不 import duckdb（R5）、不硬编码数据路径（C-4）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.audit.ledger import log_export

__all__ = ["render_research_qmd", "publish_quarto"]


def _sha256(p: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def render_research_qmd(
    conclusion: dict[str, Any],
    plan: dict[str, Any],
    out_dir: Path,
    run_id: str | None = None,
) -> Path:
    """渲染一份 [R] 研究论文 .qmd 模板。

    数字不写死在模板里：YAML params 承载结论对象计算出的实际值，
    正文用 {effect_value} / {p_value} 占位符（Quarto params 在渲染时替换）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cid = conclusion.get("conclusion_id") or run_id or f"run-{uuid.uuid4().hex[:6]}"
    path = out_dir / f"[R]{cid}.qmd"

    result = conclusion.get("result") or {}
    effect = result.get("effect") or {}
    ci = effect.get("ci95")
    status = conclusion.get("status", "")
    method = (conclusion.get("method") or {}).get("registered_name", "")
    hypothesis = (plan or {}).get("hypothesis", "")
    mde = (plan or {}).get("mde", "")

    qmd = f"""---
title: 研究结论报告（论文轨）
author: TraceableMetrics Research
date: {datetime.now():%Y-%m-%d}
format: html
params:
  plan_id: {plan.get("plan_id", "")}
  hypothesis: {json.dumps(hypothesis, ensure_ascii=False)}
  status: {status}
  method: {method}
  effect_value: {effect.get("value", "NA")}
  effect_ci: {json.dumps(ci, ensure_ascii=False) if ci else "NA"}
  p_value: {result.get("p_value", "NA")}
  mde: {mde}
---

# 研究结论

- 假设：{hypothesis}
- 执行方法：`{{method}}`（计划登记口径）
- 结论状态：`{{status}}`

## 效应量

主效应 `{{effect_value}}`（95% CI `{{effect_ci}}`），计划 MDE `{{mde}}`。

## 显著性

p 值 `{{p_value}}`。

> 声明：本模板不含任何写死的数字——所有数值均来自结论对象，由发布环节注入。
"""
    path.write_text(qmd, encoding="utf-8")
    return path


def publish_quarto(
    ledger_db: Path,
    qmd_path: Path,
    run_id: str,
    track: str = "research",
    metric: str | None = None,
) -> dict[str, Any]:
    """发布 .qmd 并记入审计流（D8：导出动作必须进 append-only 台账）。

    返回 {"export_id", "file_name", "sha256", "dest"}。
    """
    qmd = Path(qmd_path)
    if not qmd.exists():
        raise FileNotFoundError(qmd)
    export_id = f"exp-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    sha = _sha256(qmd)
    log_export(
        ledger_db, export_id=export_id, run_id=run_id, track=track,
        metric=metric, file_name=qmd.name, dest=str(qmd.parent), sha256=sha,
    )
    return {"export_id": export_id, "file_name": qmd.name, "sha256": sha,
            "dest": str(qmd.parent)}
