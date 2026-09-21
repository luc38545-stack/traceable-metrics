"""V1 端到端管道执行器（商用轨演示入口）。

一条真实 CSV 走完：raw 落地 → 体检 → dbt 声明建模（从不可变批次读）→
语义编译（嵌套指标契约）→ 指标查询（经 Semantic API，唯一取数端点）→
snapshot + run_id + provenance 全落盘（JSON 台账 + SQLite 台账双写）。

用法（在仓库根目录）：
    python -m plugins.commerce.run_v1 --csv <path-to-csv>
"""
from __future__ import annotations

# 轨道身份由 app 层决定；core 只收 TrackContext（宪法 C-4）。
import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path

import duckdb
import yaml

from core.audit import ledger
from core.audit.snapshot import publish_snapshot
from core.ingestion.context import TrackContext
from core.ingestion.land import land_file
from core.modeling.dbt import ModelingError, assert_dwd_built, run_dbt_model
from core.quality.health import run_health_check
from core.semantic.api import query_metric
from core.semantic.compiler import compile_metric, load_contract

REPO = Path(__file__).resolve().parents[2]
VOLUME = REPO / "data" / "commerce"          # app 层注入的卷根（core 不感知）
SCHEMAS = REPO / "schemas"
LEDGER_DB = REPO / "data" / "runs" / "ledger.db"
RUNS_DIR = REPO / "data" / "runs"


def run(csv_path: Path) -> dict:
    ctx = TrackContext(track="commerce", volume_root=VOLUME)
    now = datetime.now()
    run_id = f"r-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    steps: list[dict] = []
    # P0-03：失败也必须显性登记——记录当前阶段，供 write_run_failed 落事件流
    stage = {"name": "初始化", "batch_id": None}

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"  [{'OK ' if ok else 'ERR'}] {name}: {detail}")

    print(f"== TraceableMetrics V1 pipeline · run_id={run_id} · track={ctx.track} ==")

    conn = None
    try:
        # 1. raw 落地（不可变批次）
        stage["name"] = "raw 落地"
        manifest = land_file(ctx, "orders", csv_path)
        stage["batch_id"] = manifest["batch_id"]
        step("raw 落地", True, f"{manifest['batch_id']} sha256={manifest['sha256'][:12]}…")

        # 2. 体检
        stage["name"] = "体检"
        report = run_health_check(csv_path)
        step("体检", True, f"{report.rows} 行 × {len(report.columns)} 列，{len(report.issues)} 个问题")
        for iss in report.issues:
            print(f"        - [{iss.severity}] {iss.human_text} → {iss.fix_suggestion}")

        # 3. dbt 声明建模（P3：从不可变 raw 批次读，不读上传临时文件）
        stage["name"] = "dbt 建模"
        run_dbt_model(ctx.db_path, Path(manifest["dest"]), select="dwd_base")
        conn = duckdb.connect(str(ctx.db_path))
        n = assert_dwd_built(conn)
        step("dbt 建模", True, f"dwd_base {n} 行 ← {manifest['batch_id']}")

        # 3.5 断言闸门（§08.1 三级断言引擎 · D2 断言同行 · S1 红灯封下游）：
        # 行数突变带 ±40% 熔断 + 关键列唯一/非空/枚举；blocking 失败 → 管道显性失败
        from core.quality.assertions import AssertionBlocked
        from plugins.commerce.assertions_hook import run_assertion_gate
        stage["name"] = "断言闸门"
        try:
            assertion_report = run_assertion_gate(conn, RUNS_DIR)
        except AssertionBlocked as ab:
            raise ModelingError(f"断言熔断（红灯封下游）：{ab}") from ab
        step("断言闸门", True, f"{len(assertion_report['assertions'])} 条断言 · "
             f"{sum(1 for r in assertion_report['assertions'] if not r['passed'])} 条失败(记录)")

        # 4. 语义编译（契约来自 schemas/，缺失 grain/time_column 会在此抛错；
        #    ratio 契约的分子/分母嵌套引用子契约视图）
        contract_path = SCHEMAS / "metrics" / "commerce" / "pay_success_rate.yml"
        contract = load_contract(contract_path)
        view = compile_metric(conn, ctx.track, contract,
                              contracts_dir=contract_path.parent)
        step("语义编译", True, f"contract {contract['metric']}@v{contract.get('version',1)} → view {view}")

        # 5. 指标查询 —— 唯一取数端点：Semantic API（写查询日志 + freshness 返回体）
        stage["name"] = "指标查询"
        q = query_metric(conn, LEDGER_DB, contract_path, ctx.track, run_id=run_id)
        rows = q["rows"]
        step("指标查询", True, f"{len(rows)} 个日粒度点 · query_id={q['query_id']} · freshness={q['freshness']}")

        # 6. snapshot —— P0-03：每 run 一个永不覆盖的快照，原子发布
        #    （临时文件 → sha256 → rename），发布成功才写台账，杜绝「成功台账+缺失快照」
        stage["name"] = "快照发布"
        snapshot_id = f"s-{run_id.removeprefix('r-')}"
        conn.execute("CHECKPOINT;")
        conn.close()
        conn = None
        snap = publish_snapshot(ctx.db_path, ctx.snapshots_dir, snapshot_id, ctx.track)
        snap_sha = snap["sha256"]
        step("快照", True, f"{snapshot_id} sha256={snap_sha[:12]}…")

        ledger_json = {
            "schema_version": ledger.LEDGER_SCHEMA_VERSION,
            "run_id": run_id,
            "track": ctx.track,
            "started_at": now.isoformat(timespec="seconds"),
            "batch_id": manifest["batch_id"],
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snap_sha,
            "snapshot_size": snap["size"],
            "rows": n,
            "metric": contract["metric"],
            "metric_version": contract.get("version", 1),
            "metric_series": rows,
            "query_id": q["query_id"],
            "freshness": q["freshness"],
            "health_summary": report.summary,
            "assertion_report": assertion_report,
            "steps": steps,
            "provenance": {
                "run_id": run_id,
                "snapshot_id": snapshot_id,
                "batch_id": manifest["batch_id"],
                "commit_sha": ledger.build_digest(),   # 审计 #5：代码版本不可缺
                # P0-06：可复现性三件套——代码版本之外，还要锁住依赖与契约口径
                "snapshot_sha256": snap_sha,
                "dependency_lock_digest": ledger.dependency_digest(),
                "metric_contract_digest": ledger.file_digest(contract_path),
                "metric_contract_version": contract.get("version", 1),
                "dirty": ledger.build_dirty(),
                "pipeline": "plugins/commerce/run_v1.py",
                "raw_sha256": manifest["sha256"],
            },
        }
        # 双写：SQLite 台账（附录 T 冻结项⑤，机查）+ JSON 台账（人读）
        ledger.write_run(LEDGER_DB, ledger_json)
        runs_dir = REPO / "data" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        out = runs_dir / f"{run_id}.json"
        out.write_text(json.dumps(ledger_json, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"== DONE · run 台账 → {out.relative_to(REPO)} (JSON + SQLite) ==")
        return ledger_json
    except Exception as e:  # noqa: BLE001 — P0-03：任何阶段失败都必须显性登记
        # 失败 register：不写 runs 表（那里只放成功产物），进 append-only 事件流
        ledger.write_run_failed(
            LEDGER_DB, run_id, ctx.track, stage["name"],
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, type=Path)
    args = ap.parse_args()
    if not args.csv.exists():
        raise SystemExit(f"csv not found: {args.csv}")
    run(args.csv)


if __name__ == "__main__":
    main()
