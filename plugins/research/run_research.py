"""论文轨端到端管道执行器（S3 · 架构书 §六「论文轨」）。

一条研究队列 CSV 走完完整论文链：
raw 落地 → 体检 → 数据集四要素/IRB gate → 预注册 gate → 建模(dwd_cohort) →
断言闸门 → k-匿名 gate → DP 预算 → 组计数 → analyze(research_mode=True) →
claims 重算对账（C-5：对账基准来自重算，不来自模型）→ 快照 → 台账双写 →
复现包 → Quarto 出版。

轨道隔离设计（与 commerce 一致的精神，research 卷自包含）：
- duckdb 数据库：ctx.db_path（volume/research.db）
- SQLite 台账：volume/ledger.db（本轨独立台账，卷即证据袋）
- JSON 台账：volume/runs/r-*.json（喂给断言闸门的行数历史）
- 快照：volume/snapshots/s-*；复现包：volume/repro；出版：volume/exports

用法（仓库根目录）：
    python -m plugins.research.run_research --csv <csv> --manifest <dataset.json>
        --prereg <prereg.json> --prereg-id <plan_id>
"""
from __future__ import annotations

import argparse
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from core.audit import ledger
from core.audit.snapshot import publish_snapshot
from core.ingestion.context import TrackContext
from core.ingestion.land import land_file
from core.modeling.dbt import assert_model_built, run_dbt_model
from core.privacy.dp_budget import DpBudgetLedger, PrivacyBudgetExhausted
from core.privacy.kanonymity import assert_k_anonymity
from core.quality.health import run_health_check
from core.repro.package import build_repro_package
from core.statistics.executor import analyze, replay_stat
from core.claims.reconcile import reconcile

VOLUME = Path(__file__).resolve().parents[2] / "data" / "research"  # app 层注入（CLI 默认卷）


def _load_manifest(path: Path) -> dict:
    """读数据集清单（JSON 或 YAML）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if p.suffix.lower() in (".yml", ".yaml"):
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return json.loads(p.read_text(encoding="utf-8"))


def run_research_pipeline(
    ctx: TrackContext,
    raw_csv: Path,
    manifest_path: Path,
    prereg_path: Path,
    prereg_id: str,
    plan: dict,
    *,
    outcome: str = "binary",
    design: str = "rct",
    method: str | None = None,
    question: str = "",
    period: str = "",
    mde: float | None = None,
    alpha: float = 0.05,
    power: float = 0.8,
    sample_a: list[float] | None = None,
    sample_b: list[float] | None = None,
    dp_budget_path: Path | None = None,
    dp_epsilon: float = 0.1,
    suite: str = "full",
) -> dict[str, Any]:
    """跑一次完整论文轨 run。任何闸门失败 → 显性异常（P4）+ 失败事件落账。

    计数永远来自数据本身（group_counts 从 dwd_cohort 折叠），调用方不得喂 counts——
    这是论文轨的诚实边界：结论数字必须可回溯到原始队列。
    """
    now = datetime.now()
    run_id = f"r-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    snapshot_id = f"s-{run_id.removeprefix('r-')}"
    steps: list[dict] = []
    stage = {"name": "初始化", "batch_id": None}

    # 卷目录（app 层选择数据位置，C-4）
    vol = ctx.volume_root
    for sub in ("raw", "snapshots", "exports", "runs", "repro"):
        (vol / sub).mkdir(parents=True, exist_ok=True)
    ledger_db = vol / "ledger.db"
    runs_dir = vol / "runs"

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"  [{'OK ' if ok else 'ERR'}] {name}: {detail}")

    print(f"== TraceableMetrics Research pipeline · run_id={run_id} · track={ctx.track} ==")
    conn = None
    try:
        # 1. raw 落地（不可变批次）
        stage["name"] = "raw 落地"
        batch = land_file(ctx, "cohort", raw_csv)
        stage["batch_id"] = batch["batch_id"]
        step("raw 落地", True, f"{batch['batch_id']} sha256={batch['sha256'][:12]}…")

        # 2. 体检
        stage["name"] = "体检"
        report = run_health_check(raw_csv)
        step("体检", True, f"{report.rows} 行 × {len(report.columns)} 列，{len(report.issues)} 个问题")

        # 3. 数据集四要素 + IRB gate（论文轨强制）
        from plugins.research.dataset_gate import check_dataset_gate

        stage["name"] = "数据集 gate"
        dataset = _load_manifest(manifest_path)
        gate = check_dataset_gate(dataset)
        step("数据集 gate", True, f"{gate['dataset_name']}@{gate['version']} IRB={gate['irb']['approval_no']}")

        # 4. 预注册 gate（先登记后执行；口径与计划逐字一致）
        from core.statistics.method_contract import (
            MethodRegistry,
            candidate_families,
        )
        from core.statistics.prereg import PreRegistrationLedger
        from plugins.research.prereg_hook import require_preregistered

        stage["name"] = "预注册 gate"
        chosen = method or candidate_families(outcome, design)[0]
        prereg_ledger = PreRegistrationLedger(prereg_path)
        plan_registered = require_preregistered(prereg_ledger, prereg_id, {
            "outcome": outcome, "design": design, "method": chosen,
            "alpha": alpha, "mde": mde, "power": power,
        })
        # 方法集冻结（S3「登记册 + 冻结开关」）：口径对齐后冻结本次 run 的登记册，
        # 分析期间方法集不可变，指纹进台账——杜绝「边跑边改方法」的事后合理化。
        run_registry = MethodRegistry(frozen=True)
        step("预注册 gate", True,
             f"plan {prereg_id} 口径一致 ✓ · 方法集冻结 fp="
             f"{run_registry.fingerprint()[:12]}…")

        # 5. 建模：raw → dwd_cohort（dbt 声明式，§04-L3；从不可变批次读，P3）
        #    --select 必传 dwd_cohort：双轨共用同一 dbt 工程，落回默认 select
        #    会把 commerce 的 dwd_base 一起跑进 research 库（模型串门）。
        stage["name"] = "建模"
        run_dbt_model(ctx.db_path, batch["dest"], select="dwd_cohort")
        conn = duckdb.connect(str(ctx.db_path))
        n_rows = assert_model_built(conn, "dwd_cohort", min_rows=1)
        step("建模", True, f"dwd_cohort {n_rows} 行 ← {batch['batch_id']}"
                           f"（dbt --select dwd_cohort）")

        # 6. 断言闸门（participant_id 唯一 blocking；arm/converted 枚举 non-blocking）
        from core.quality.assertions import AssertionBlocked
        from plugins.research.assertions_hook import run_research_assertion_gate

        stage["name"] = "断言闸门"
        try:
            assertion_report = run_research_assertion_gate(conn, runs_dir)
        except AssertionBlocked as ab:
            raise RuntimeError(f"断言熔断（红灯封下游）：{ab}") from ab
        step("断言闸门", True, f"{len(assertion_report['assertions'])} 条断言 · "
             f"{sum(1 for r in assertion_report['assertions'] if not r['passed'])} 条失败(记录)")

        # 7. k-匿名 gate（清单声明了准标识符与 k → 强制执行）
        privacy = dataset.get("privacy") or {}
        qids = privacy.get("quasi_identifiers") or []
        k_val = privacy.get("k")
        if qids and k_val:
            stage["name"] = "k-匿名 gate"
            assert_k_anonymity(conn, "dwd_cohort", qids, int(k_val))
            step("k-匿名 gate", True, f"k={k_val} 满足（准标识符 {qids}）")

        # 8. DP 预算（可选：提供了预算账本路径才记账）
        dp_remaining = None
        if dp_budget_path is not None:
            stage["name"] = "DP 预算"
            dp = DpBudgetLedger(dp_budget_path, total_epsilon=float(plan.get("epsilon") or 1.0))
            try:
                dp_remaining = dp.spend(f"spend-{run_id}", epsilon=dp_epsilon,
                                        purpose=f"run {run_id} 统计披露")
            except PrivacyBudgetExhausted:
                raise
            step("DP 预算", True, f"spend ε={dp_epsilon} → 剩余 {dp_remaining:.4f}")

        # 9. 组计数（计数来自数据本身）
        from plugins.research.inputs import group_counts

        stage["name"] = "组计数"
        counts_dict = group_counts(conn)
        counts = counts_dict["counts"]
        step("组计数", True, f"counts={counts}")

        # 10. 统计分析（research_mode=True：诊断书追加识别假设与安慰剂模板）
        stage["name"] = "统计分析"
        chosen = method
        source = {"query_id": f"q-{run_id}", "run_id": run_id, "snapshot_id": snapshot_id}
        conclusion = analyze(
            outcome, design, ctx.track,
            counts=counts, sample_a=sample_a, sample_b=sample_b,
            method=chosen, suite=suite, mde=mde, alpha=alpha, power=power,
            question=question, period=period, source=source,
            run_id=run_id, research_mode=True, registry=run_registry,
        )
        step("统计分析", True,
             f"status={conclusion['status']} method={conclusion['method']['registered_name']}")

        # 11. claims 重算对账（C-5：基准来自重算；FAIL → 显性失败，报告不渲染）
        rec: dict[str, Any] = {"reconcile_diff": "skipped", "quality_gate": "n/a"}
        if conclusion["result"] is not None:
            stage["name"] = "claims 对账"
            rec = reconcile(conclusion["claims"], replay_stat(conclusion, counts=counts))
            if rec["reconcile_diff"] != "clean":
                raise RuntimeError(f"claims 对账失败（报告不渲染）：{rec['failed']}")
            step("claims 对账", True, f"{len(rec['claims'])} 条 claim 全部 PASS")

        # 12. 快照发布（唯一、原子、不可覆盖）
        stage["name"] = "快照发布"
        conn.execute("CHECKPOINT;")
        conn.close()
        conn = None
        snap = publish_snapshot(ctx.db_path, ctx.snapshots_dir, snapshot_id, ctx.track)
        step("快照发布", True, f"{snapshot_id} sha256={snap['sha256'][:12]}…")

        # 13. 台账双写（SQLite 机查 + JSON 人读；provenance 四元组 + prereg_id 事件）
        stage["name"] = "台账"
        ledger_json = {
            "schema_version": ledger.LEDGER_SCHEMA_VERSION,
            "run_id": run_id,
            "track": ctx.track,
            "started_at": now.isoformat(timespec="seconds"),
            "batch_id": batch["batch_id"],
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snap["sha256"],
            "rows": n_rows,
            "metric": plan.get("primary_metric") or conclusion["method"]["registered_name"],
            "metric_version": 1,
            "health_summary": report.summary,
            "assertion_report": assertion_report,
            "dataset_gate": gate,
            "conclusion_status": conclusion["status"],
            "counts": list(counts),
            "reconcile": rec,
            "prereg_id": prereg_id,
            "dp_remaining": dp_remaining,
            "steps": steps,
            "provenance": {
                "run_id": run_id,
                "snapshot_id": snapshot_id,
                "batch_id": batch["batch_id"],
                "commit_sha": ledger.build_digest(),
                "methods_fingerprint": run_registry.fingerprint(),
                "snapshot_sha256": snap["sha256"],
                "dependency_lock_digest": ledger.dependency_digest(),
                "dirty": ledger.build_dirty(),
                "pipeline": "plugins/research/run_research.py",
                "raw_sha256": batch["sha256"],
            },
        }
        ledger.write_run(ledger_db, ledger_json)
        ledger.log_event(ledger_db, "prereg_checked", subject_id=run_id,
                         track=ctx.track, payload={"prereg_id": prereg_id,
                                                   "plan_id": plan.get("plan_id")})
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / f"{run_id}.json").write_text(
            json.dumps(ledger_json, ensure_ascii=False, indent=2), encoding="utf-8")
        step("台账", True, f"SQLite={ledger_db.name} + JSON r-*.json")

        # 14. 复现包（证据袋：raw/snapshot/prereg/dataset/依赖/代码指纹）
        stage["name"] = "复现包"
        repro = build_repro_package(
            ledger_json, vol, vol / "repro", Path(batch["dest"]),
            ctx.db_path, prereg_path, manifest_path)
        step("复现包", True, f"{Path(repro['package_dir']).name} · sha256={repro['sha256'][:12]}…")

        # 15. Quarto 出版（[R] 文件 + log_export 审计）
        from plugins.research.quarto import publish_quarto, render_research_qmd

        stage["name"] = "Quarto 出版"
        qmd = render_research_qmd(conclusion, plan_registered,
                                  vol / "exports", run_id=run_id)
        pub = publish_quarto(ledger_db, qmd, run_id, track=ctx.track,
                             metric=ledger_json["metric"])
        step("Quarto 出版", True, f"{pub['file_name']} sha256={pub['sha256'][:12]}…")

        print(f"== DONE · run {run_id} 全链路闭合（gate→快照→台账→复现包→Quarto）==")
        return {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "batch_id": batch["batch_id"],
            "ledger_db": ledger_db,
            "runs_dir": runs_dir,
            "conclusion": conclusion,
            "counts": tuple(counts),
            "group_counts": counts_dict,
            "reconcile": rec,
            "repro_package": repro["package_dir"],
            "qmd_path": qmd,
            "dp_remaining": dp_remaining,
            "steps": steps,
        }
    except Exception as e:  # noqa: BLE001 — P0-03：任何阶段失败都必须显性登记
        ledger.write_run_failed(
            ledger_db, run_id, ctx.track, stage["name"],
            type(e).__name__, str(e),
            batch_id=stage.get("batch_id"),
            started_at=now.isoformat(timespec="seconds"),
        )
        step(stage["name"], False, f"{type(e).__name__}: {e}")
        print(f"== FAILED · run {run_id} 已登记失败事件（stage={stage['name']}）==")
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description="论文轨端到端管道")
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--prereg", required=True, type=Path)
    ap.add_argument("--prereg-id", required=True)
    ap.add_argument("--volume", type=Path, default=VOLUME)
    ap.add_argument("--method", default="two_proportion_test")
    ap.add_argument("--design", default="rct")
    ap.add_argument("--outcome", default="binary")
    ap.add_argument("--question", default="队列转化率组间差异")
    ap.add_argument("--period", default="2026-08")
    ap.add_argument("--mde", type=float, default=0.08)
    args = ap.parse_args()
    for p in (args.csv, args.manifest, args.prereg):
        if not p.exists():
            raise SystemExit(f"file not found: {p}")
    plan = json.loads(Path(args.prereg).read_text(encoding="utf-8"))["records"][args.prereg_id]["plan"]
    ctx = TrackContext(track="research", volume_root=args.volume)
    run_research_pipeline(
        ctx=ctx, raw_csv=args.csv, manifest_path=args.manifest,
        prereg_path=args.prereg, prereg_id=args.prereg_id, plan=plan,
        outcome=args.outcome, design=args.design, method=args.method,
        question=args.question, period=args.period, mde=args.mde,
    )


if __name__ == "__main__":
    main()
