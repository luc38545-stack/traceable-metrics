#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 可信度闭环回归测试（整改意见 P0-01 / 02 / 03 / 05 / 06 / 07）。

每条用例都对应整改文档「必增测试」里点名的一条，且都满足文档第 4 条铁律：
**在旧实现上必须失败**。它们各自盯住的旧缺陷是——

- P0-01：`NaN` 参与比较恒为 False，会静默落到 PASS 分支；`True` 会被 `float()` 当成 1.0；
- P0-02：只校验 source 三个字段非空，伪造 query_id / 交叉绑定都能过关；
- P0-03：`snapshot_id` 只到日期，同日两次运行覆盖同一个 `commerce.db`；
- P0-05：`INSERT OR REPLACE` 允许同一 run_id 覆盖历史台账；
- P0-06：`batch_id` 顶替 `commit_sha`，数据批次无法说明用了哪版代码；
- P0-07：`ledger.get("track") or "commerce"` 把损坏对象静默标成商用轨。

运行：仓库根目录下  pytest -q tests/test_p0_credibility.py
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH

from core.audit import ledger
from core.audit.ledger import LedgerError, write_run
from core.audit.snapshot import SnapshotError, publish_snapshot
from core.claims.reconcile import ClaimError, range_from_contract, reconcile, validate_claim
from core.claims.source import SourceRequest, reconcile_verified
from core.report.kernel import ReportBlocked, build_conclusion, render_html
from core.report.schema import TRACKS, ConclusionSchemaError, validate_conclusion

METRIC = "pay_success_rate"
TRACK = "commerce"


# ---------------------------------------------------------------- 公共夹具

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_snapshot(path: Path, series=(("2026-08-26", 0.875),)) -> Path:
    """造一个最小可用快照库：含 `pay_success_rate` 视图，供重放取数。"""
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        # 幂等：同一 src 文件可被重复打开改写（模拟「第二次运行数据不同」）
        con.execute(
            "CREATE TABLE IF NOT EXISTS dwd_base (created_at TIMESTAMP, "
            "amount_total DOUBLE, amount_paid DOUBLE, status VARCHAR)"
        )
        con.execute(f"DROP VIEW IF EXISTS {METRIC}")
        vals = ", ".join(f"(DATE '{d}', {v}::DOUBLE)" for d, v in series)
        con.execute(f"CREATE VIEW {METRIC} AS SELECT * FROM (VALUES {vals}) t(dt, value)")
    finally:
        con.close()
    return path


def _run_record(run_id: str, snapshot_id: str, snapshot_sha: str) -> dict:
    return {
        "run_id": run_id,
        "track": TRACK,
        "started_at": "2026-08-30T10:00:00",
        "batch_id": "b-1",
        "snapshot_id": snapshot_id,
        "snapshot_sha256": snapshot_sha,
        "rows": 8,
        "metric": METRIC,
        "metric_version": 2,
        "health_summary": {},
        "steps": [],
        "provenance": {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "batch_id": "b-1",
            "commit_sha": "a" * 64,
        },
    }


@pytest.fixture()
def env(tmp_path):
    """一个「全部真实且互相匹配」的溯源环境：台账 + 快照 + 一次真实的语义查询。"""
    db = tmp_path / "ledger.db"
    snap = _make_snapshot(tmp_path / "s-1" / "commerce.db")
    sha = _sha(snap)
    write_run(db, _run_record("r-1", "s-1", sha))
    ledger.log_query(db, "q-1", METRIC, TRACK, run_id="r-1")
    return SimpleNamespace(db=db, snap=snap, sha=sha, query_id="q-1",
                           run_id="r-1", snapshot_id="s-1")


def _req(env, **over) -> SourceRequest:
    base = dict(ledger_db=env.db, snapshot_file=env.snap, query_id=env.query_id,
                run_id=env.run_id, snapshot_id=env.snapshot_id,
                track=TRACK, metric=METRIC)
    base.update(over)
    return SourceRequest(**base)


def _claims(value=0.875, period="2026-08-26", run_id="r-1", snapshot_id="s-1",
            query_id="q-1") -> list[dict]:
    return [{
        "metric": METRIC, "period": period, "operation": "point",
        "value": value, "unit": "ratio",
        "source": {"run_id": run_id, "snapshot_id": snapshot_id, "query_id": query_id},
    }]


# ---------------------------------------------------------------- P0-01

@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), float("-inf"),
    True, False,                       # float(True)==1.0，不显式拒绝就会混进对账
    "0.875", None, "abc", [],
])
def test_p001_non_finite_and_non_numeric_values_rejected(bad):
    """整改 P0-01：NaN / Infinity / bool / 非数值必须被拒，绝不能落到 PASS。"""
    with pytest.raises(ClaimError):
        validate_claim(_claims(value=bad)[0])


def test_p001_nan_would_silently_pass_naive_comparison():
    """元测试：证明「不拒绝 NaN」确实会静默通过——这条用例是修复动因本身。"""
    nan = float("nan")
    # 旧实现的核心比较：abs(nan - 0.875) >= 1e-9 恒为 False → 误判 PASS
    assert not (abs(nan - 0.875) >= 1e-9)
    # 因此必须在校验阶段就拦掉
    with pytest.raises(ClaimError):
        validate_claim(_claims(value=nan)[0])


@pytest.mark.parametrize("replay_value", [float("nan"), float("inf"), float("-inf")])
def test_p001_replay_non_finite_is_fail(replay_value):
    """重放值非有限 → 显式 FAIL（不得包装成 PASS）。"""
    out = reconcile(_claims(value=0.875), {"2026-08-26": replay_value})
    assert out["quality_gate"] == "blocked"
    assert out["reconcile_diff"] == "MISMATCH"
    assert out["claims"][0]["reconcile"] == "FAIL"


def test_p001_plain_tamper_still_mismatch():
    """普通篡改依旧 FAIL——修 NaN 不能把正常对账修坏。"""
    out = reconcile(_claims(value=0.9999), {"2026-08-26": 0.875})
    assert out["quality_gate"] == "blocked"
    assert out["claims"][0]["reconcile"] == "FAIL"


def test_p001_contract_range_enforced_when_declared():
    """范围只能由 Metric Contract 声明，核心不写死；声明了就必须执行。"""
    rng = range_from_contract({"value_range": [0, 1]})
    assert rng == (0.0, 1.0)
    with pytest.raises(ClaimError):
        validate_claim(_claims(value=1.5)[0], unit_range=rng)
    validate_claim(_claims(value=1.0)[0], unit_range=rng)   # 边界内放行
    assert range_from_contract({}) is None                  # 没声明就不校验


# ---------------------------------------------------------------- P0-02

def test_p002_all_real_and_matching_passes(env):
    """唯一允许 PASS 的情形：source 每一环都真实且互相匹配。"""
    out = reconcile_verified(_claims(), _req(env))
    assert out["source_proof"]["ok"] is True
    assert out["quality_gate"] == "passed"
    assert out["reconcile_diff"] == "clean"


def test_p002_forged_query_id_rejected(env):
    out = reconcile_verified(_claims(query_id="q-FORGED"), _req(env, query_id="q-FORGED"))
    assert out["quality_gate"] == "blocked"
    assert out["reconcile_diff"] != "clean"
    assert "query_exists" in out["source_proof"]["reason"]


def test_p002_query_belongs_to_another_run_rejected(env):
    ledger.log_query(env.db, "q-other-run", METRIC, TRACK, run_id="r-OTHER")
    out = reconcile_verified(_claims(query_id="q-other-run"),
                             _req(env, query_id="q-other-run"))
    assert out["quality_gate"] == "blocked"
    assert "query_run_match" in out["source_proof"]["reason"]


def test_p002_query_belongs_to_another_track_rejected(env):
    ledger.log_query(env.db, "q-other-track", METRIC, "research", run_id="r-1")
    out = reconcile_verified(_claims(query_id="q-other-track"),
                             _req(env, query_id="q-other-track"))
    assert out["quality_gate"] == "blocked"
    assert "query_track_match" in out["source_proof"]["reason"]


def test_p002_metric_mismatch_rejected(env):
    ledger.log_query(env.db, "q-other-metric", "refund_rate", TRACK, run_id="r-1")
    out = reconcile_verified(_claims(query_id="q-other-metric"),
                             _req(env, query_id="q-other-metric"))
    assert out["quality_gate"] == "blocked"
    assert "query_metric_match" in out["source_proof"]["reason"]


def test_p002_snapshot_id_mismatch_rejected(env):
    out = reconcile_verified(_claims(snapshot_id="s-WRONG"),
                             _req(env, snapshot_id="s-WRONG"))
    assert out["quality_gate"] == "blocked"
    assert "run_snapshot_match" in out["source_proof"]["reason"]


def test_p002_tampered_snapshot_sha_rejected(env):
    """快照文件被替换（实时哈希 ≠ 台账记录）→ 拒绝对账。"""
    env.snap.write_bytes(env.snap.read_bytes() + b"tamper")   # 改动快照字节
    out = reconcile_verified(_claims(), _req(env))
    assert out["quality_gate"] == "blocked"
    assert "snapshot_sha_match" in out["source_proof"]["reason"]


def test_p002_via_semantic_zero_rejected(env):
    """绕过语义层的取数不算可信来源。"""
    ledger.log_query(env.db, "q-direct", METRIC, TRACK, run_id="r-1", via_semantic=False)
    out = reconcile_verified(_claims(query_id="q-direct"), _req(env, query_id="q-direct"))
    assert out["quality_gate"] == "blocked"
    assert "via_semantic" in out["source_proof"]["reason"]


def test_p002_missing_snapshot_file_rejected(env):
    """P0-04：目标快照缺失，即使目录里躺着别的快照也不许拿去顶替。"""
    _make_snapshot(env.snap.parent.parent / "s-OTHER" / "commerce.db")  # 别轨快照
    env.snap.unlink()
    out = reconcile_verified(_claims(), _req(env))
    assert out["quality_gate"] == "blocked"
    assert "snapshot_exists" in out["source_proof"]["reason"]


def test_p002_blocked_source_writes_audit_event(env):
    """source 验证失败必须落 append-only 审计事件（P0-02 第 7 条 / P4）。"""
    reconcile_verified(_claims(query_id="q-FORGED"), _req(env, query_id="q-FORGED"))
    con = sqlite3.connect(str(env.db))
    try:
        rows = con.execute(
            "SELECT event_type, subject_id FROM events WHERE event_type = 'claims_blocked'"
        ).fetchall()
    finally:
        con.close()
    assert rows and rows[0][1] == "r-1"


def test_p002_tampered_value_fails_even_with_real_source(env):
    """source 全真、数值被改 → 依旧 FAIL（对账本身不能被绕过）。"""
    out = reconcile_verified(_claims(value=0.9999), _req(env))
    assert out["source_proof"]["ok"] is True
    assert out["quality_gate"] == "blocked"


# ---------------------------------------------------------------- P0-03

def test_p003_each_publish_creates_distinct_snapshot(tmp_path):
    """同一天连续两次发布 → 两个快照目录，互不覆盖。"""
    src = _make_snapshot(tmp_path / "src" / "commerce.db",
                         series=(("2026-08-26", 0.5),))
    root = tmp_path / "snapshots"
    a = publish_snapshot(src, root, "s-20260830-000000-aaaa", TRACK)
    _make_snapshot(src, series=(("2026-08-26", 0.9),))          # 数据变了
    b = publish_snapshot(src, root, "s-20260830-000001-bbbb", TRACK)

    assert a["path"] != b["path"]
    assert a["sha256"] != b["sha256"]
    assert (root / "s-20260830-000000-aaaa" / "commerce.db").exists()
    assert (root / "s-20260830-000001-bbbb" / "commerce.db").exists()


def test_p003_first_snapshot_unchanged_after_second_publish(tmp_path):
    """第一次的快照哈希，在第二次运行之后必须保持不变。"""
    src = _make_snapshot(tmp_path / "src" / "commerce.db")
    root = tmp_path / "snapshots"
    first = publish_snapshot(src, root, "s-1", TRACK)
    sha_before = _sha(Path(first["path"]))

    _make_snapshot(src, series=(("2026-08-26", 0.111),))        # 第二次数据不同
    publish_snapshot(src, root, "s-2", TRACK)

    assert _sha(Path(first["path"])) == sha_before == first["sha256"]


def test_p003_duplicate_snapshot_id_rejected(tmp_path):
    """重复 snapshot_id 必须排他失败——旧实现会静默覆盖。"""
    src = _make_snapshot(tmp_path / "src" / "commerce.db")
    root = tmp_path / "snapshots"
    publish_snapshot(src, root, "s-1", TRACK)
    with pytest.raises(SnapshotError):
        publish_snapshot(src, root, "s-1", TRACK)


def test_p003_failed_publish_leaves_no_half_snapshot(tmp_path):
    """发布失败不留残骸（避免「半个快照」被后续对账读到）。"""
    root = tmp_path / "snapshots"
    with pytest.raises(SnapshotError):
        publish_snapshot(tmp_path / "missing.db", root, "s-1", TRACK)
    assert not (root / "s-1").exists()


def test_p003_two_real_runs_produce_two_snapshots():
    """真实管道连跑两次 → 两个 run、两个快照，且第一次的哈希不变。

    这条跑真管道（dbt + duckdb），约 20 秒，用 -k 可单独挑选。
    """
    from plugins.commerce import run_v1

    csv = ROOT / "examples" / "测试数据-8月26日订单.csv"
    if not csv.exists():
        pytest.skip(f"演示数据缺失：{csv}")

    a = run_v1.run(csv)
    snap_a = run_v1.VOLUME / "snapshots" / a["snapshot_id"] / "commerce.db"
    sha_a = _sha(snap_a)

    b = run_v1.run(csv)

    assert a["run_id"] != b["run_id"]
    assert a["snapshot_id"] != b["snapshot_id"]
    assert _sha(snap_a) == sha_a == a["snapshot_sha256"]       # 第一次未被覆盖
    assert (run_v1.VOLUME / "snapshots" / b["snapshot_id"] / "commerce.db").exists()
    # 同一份输入 → 同一份口径（重跑得到原指标）
    assert a["metric_series"] == b["metric_series"] == [["2026-08-26", 0.875]]


# ---------------------------------------------------------------- P0-05

def test_p005_duplicate_run_id_rejected_and_first_untouched(tmp_path):
    """同一 run_id 第二次写入 → 拒绝，且首条记录一字不改。"""
    db = tmp_path / "ledger.db"
    rec = _run_record("r-1", "s-1", "0" * 64)
    write_run(db, rec)

    con = sqlite3.connect(str(db))
    before = con.execute("SELECT * FROM runs WHERE run_id='r-1'").fetchone()
    con.close()

    rec2 = dict(rec, rows=999)                                  # 试图改写历史
    with pytest.raises(LedgerError):
        write_run(db, rec2)

    con = sqlite3.connect(str(db))
    after = con.execute("SELECT * FROM runs WHERE run_id='r-1'").fetchone()
    n_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    con.close()
    assert before == after, "append-only：历史记录被改写了"
    assert n_runs == 1


def test_p005_failed_run_recorded_as_event_only(tmp_path):
    """失败 run 落 append-only 事件，不进 runs 表（那里只放成功产物）。"""
    db = tmp_path / "ledger.db"
    ledger.write_run_failed(db, "r-FAIL", TRACK, stage="快照发布",
                            error_type="SnapshotError", error="磁盘已满",
                            batch_id="b-1", started_at="2026-08-30T10:00:00")
    con = sqlite3.connect(str(db))
    try:
        ev = con.execute("SELECT event_type, subject_id, payload FROM events").fetchall()
        n_runs = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        con.close()
    assert n_runs == 0
    assert ev and ev[0][0] == "run_failed"
    assert "快照发布" in ev[0][2] and "SnapshotError" in ev[0][2]


def test_p005_queries_exports_and_events_are_auditable(tmp_path):
    """查询、导出、通用事件都必须可追溯。"""
    db = tmp_path / "ledger.db"
    ledger.log_query(db, "q-1", METRIC, TRACK, run_id="r-1")
    ledger.log_export(db, "e-1", "r-1", TRACK, METRIC, "report.html", "/tmp/x", "a" * 64)
    ledger.log_event(db, "authz_denied", subject_id="k-1", track=TRACK, payload={"a": 1})

    con = sqlite3.connect(str(db))
    try:
        types = {r[0] for r in con.execute("SELECT event_type FROM events")}
    finally:
        con.close()
    assert {"query", "export", "authz_denied"} <= types


def test_p005_ledger_source_has_no_update_delete_or_replace():
    """静态自检：台账代码里不许出现 UPDATE / DELETE / INSERT OR REPLACE。"""
    src = (ROOT / "core" / "audit" / "ledger.py").read_text(encoding="utf-8")
    low = src.lower()
    for forbidden in ("update ", "delete ", "insert or "):
        assert forbidden not in low, f"台账代码出现写覆盖语句：{forbidden!r}"


# ---------------------------------------------------------------- P0-06

def test_p006_provenance_helpers_are_honest():
    """代码版本 / 依赖锁 / 契约摘要三者齐备，dirty 判定不许伪装成 clean。"""
    assert len(ledger.build_digest()) == 64
    assert len(ledger.dependency_digest()) == 64
    assert len(ledger.file_digest(ROOT / "requirements.lock.txt")) == 64
    dirty = ledger.build_dirty()
    assert dirty in ("true", "false") or dirty.startswith("unknown"), dirty


def test_p006_provenance_requires_code_version():
    """batch_id 是数据批次，不能顶替代码版本（P0-06 的根因）。"""
    assert "commit_sha" in ledger.REQUIRED_PROVENANCE
    with pytest.raises(LedgerError):
        write_run(Path("__none__") / "x.db", {          # 缺 commit_sha → 拒收
            "run_id": "r-1", "track": TRACK, "started_at": "2026-08-30T10:00:00",
            "provenance": {"run_id": "r-1", "snapshot_id": "s-1", "batch_id": "b-1"},
        })


def test_p006_real_run_provenance_locks_code_deps_contract():
    """真实跑出来的 run，必须能唯一定位代码版本、依赖、契约与快照。"""
    from plugins.commerce import run_v1

    csv = ROOT / "examples" / "测试数据-8月26日订单.csv"
    if not csv.exists():
        pytest.skip(f"演示数据缺失：{csv}")
    rec = run_v1.run(csv)
    prov = rec["provenance"]
    for key in ("commit_sha", "dependency_lock_digest", "metric_contract_digest",
                "metric_contract_version", "dirty", "snapshot_sha256",
                "raw_sha256", "batch_id", "snapshot_id", "run_id", "pipeline"):
        assert prov.get(key), f"provenance 缺 {key}"
    assert len(prov["commit_sha"]) == 64
    assert len(prov["metric_contract_digest"]) == 64


# ---------------------------------------------------------------- P0-07

def _clean_claims_result():
    return {
        "claims": [{
            "metric": METRIC, "period": "2026-08-26", "operation": "point",
            "value": 0.875, "unit": "ratio",
            "source": {"run_id": "r-1", "snapshot_id": "s-1", "query_id": "q-1"},
            "reconcile": "PASS",
        }],
        "reconcile_diff": "clean",
        "quality_gate": "passed",
        "failed": [],
    }


def _good_ledger():
    return {
        "run_id": "r-1", "track": TRACK, "metric": METRIC, "metric_version": 2,
        "rows": 8, "snapshot_id": "s-1", "health_summary": {},
        "metric_series": [["2026-08-26", 0.875]],
        "provenance": {"run_id": "r-1", "snapshot_id": "s-1",
                       "batch_id": "b-1", "commit_sha": "a" * 64},
    }


def test_p007_valid_conclusion_passes():
    c = build_conclusion(_good_ledger(), _clean_claims_result())
    assert c["track"] == TRACK
    assert render_html(c).startswith("<!DOCTYPE html>")


@pytest.mark.parametrize("bad_track", [None, "", "research2", "COMMERCE", "default"])
def test_p007_missing_or_illegal_track_rejected(bad_track):
    """缺 track / 空 track / 非法 track → 拒绝，绝不回落 commerce。"""
    led = _good_ledger()
    if bad_track is None:
        led.pop("track")
    else:
        led["track"] = bad_track
    with pytest.raises((ReportBlocked, ConclusionSchemaError)):
        build_conclusion(led, _clean_claims_result())


def test_p007_no_commerce_fallback_in_kernel_source():
    """静态自检：内核里不许再出现 `or "commerce"` 这种静默补全。"""
    src = (ROOT / "core" / "report" / "kernel.py").read_text(encoding="utf-8")
    assert 'or "commerce"' not in src


@pytest.mark.parametrize("missing", ["run_id", "snapshot_id", "batch_id", "commit_sha"])
def test_p007_provenance_gap_rejected(missing):
    prov = {"run_id": "r-1", "snapshot_id": "s-1", "batch_id": "b-1", "commit_sha": "a" * 64}
    prov.pop(missing)
    led = _good_ledger()
    led["provenance"] = prov
    with pytest.raises((ReportBlocked, ConclusionSchemaError)):
        build_conclusion(led, _clean_claims_result())


@pytest.mark.parametrize("bad_track", [None, "", "research2"])
def test_p007_render_html_rejects_bad_track(bad_track):
    """渲染入口必须复用同一个校验器，不能只管 build_conclusion。"""
    c = build_conclusion(_good_ledger(), _clean_claims_result())
    if bad_track is None:
        c.pop("track")
    else:
        c["track"] = bad_track
    with pytest.raises(ReportBlocked):
        render_html(c)


def test_p007_blocked_reports_produce_no_track_file(tmp_path):
    """验收标准：不得生成 `[C]` / `[R]` 文件（拒绝发生在写文件之前）。"""
    from core.report.export import export_report

    # 传非法 track 的台账给导出器——build_conclusion 的校验必须拦在写文件之前
    bad_ledger = _good_ledger()
    bad_ledger["track"] = "research2"
    with pytest.raises(ReportBlocked):
        export_report(bad_ledger, _clean_claims_result(), out_dir=tmp_path,
                      ledger_db=tmp_path / "ledger.db")
    assert list(tmp_path.glob("[CR]*")) == []
