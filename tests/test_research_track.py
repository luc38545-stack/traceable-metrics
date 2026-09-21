#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S3 论文轨测试（架构书 §六 S3 · D5 统计诚实底线 · D3 溯源 · D8 审计 · 复现性）。

对照冻结路线图（§六「S3 论文轨」）逐组件落测：

  R1-R4   预注册（pre-registration）：计划指纹 / 执行-计划一致性 / append-only 台账 / 缺失拦截
  R5-R8   研究数据集四要素 + IRB 钩子（dataset gate）
  R9-R11  k-匿名（k-anonymity）：满足 / 违反 / 标识符安全
  R12-R14 差分隐私预算账本（DP budget ledger）：记账结余 / 超预算 / append-only
  R15-R17 复现包（repro package）：构建校验 / 篡改诚实报告 / 缺溯源拒绝
  R18-R19 完整统计套件诚实边界：count 族显式失败（不静默）/ research_mode 诊断完备度
  R20     Quarto 出版：[R] 文件、数字走插值
  R21     论文轨端到端：raw→gate→预注册→断言→组计数→analyze→快照→台账→复现包

casekit 约定（重要）：REJECT_CASES / FAILMSG_CASES 的函数**必须让异常自然向上抛**，
不得在函数内 try/except 后 return True——否则 casekit 会误判为「未抛异常」。

实现纪律（测试先行 RED）：本文件先落盘，对应模块尚未实现时逐条 FAIL；
实现顺序为 #37 prereg → #39 privacy → #40 repro → #36 research 插件 → #41 run_research。

运行：仓库根目录下  python tests/test_research_track.py  或  pytest -q tests/test_research_track.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH（README 已记录）
sys.path.insert(0, str(Path(__file__).resolve().parent))  # P2-01：稳定 import casekit

import json
import tempfile

import casekit
from core.statistics.executor import analyze
from core.statistics.method_contract import MethodContractError


# ---------------------------------------------------------------- 共享夹具

def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="traceable_r_"))


def _make_plan() -> dict:
    """一份合法预注册计划（AnalysisPlan 的 dict 形态）。"""
    return {
        "plan_id": "pre-test-001",
        "hypothesis": "处理组转化率高于对照组",
        "outcome": "binary",
        "design": "rct",
        "method": "two_proportion_test",
        "alpha": 0.05,
        "mde": 0.08,
        "power": 0.8,
        "primary_metric": "conversion_rate",
        "epsilon": 1.0,
    }


def _manifest(expires: str = "2099-12-31", approved: bool = True,
              missing: str | None = None) -> dict:
    """研究数据集清单（四要素 + IRB 块）。missing 指定后剔除该要素。"""
    m = {
        "dataset_name": "cohort-2026",
        "version": "1.0.0",
        "owner": "research-lab",
        "collection_method": "platform log extraction, de-identified at source",
        "irb": {
            "approved": approved,
            "expires_at": expires,
            "approval_no": "IRB-2026-0088",
        },
        "privacy": {"quasi_identifiers": ["city", "age"], "k": 2,
                    "dp_epsilon": 1.0},
    }
    if missing:
        m.pop(missing, None)
    return m


def _cohort_csv(path: Path, rows: list[tuple], header: tuple = (
        "participant_id", "arm", "converted", "city", "age")) -> Path:
    lines = [",".join(header)]
    lines += [",".join(str(v) for v in r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- R1-R4 预注册

def r1_plan_fingerprint_stable() -> bool:
    """同一计划两次指纹一致；改一个字段必须变指纹（D3：可复现判据）。"""
    from core.statistics.prereg import plan_fingerprint

    p1, p2 = _make_plan(), _make_plan()
    assert plan_fingerprint(p1) == plan_fingerprint(p2), "同一计划指纹必须稳定"
    p2["method"] = "fisher"
    assert plan_fingerprint(p1) != plan_fingerprint(p2), "计划变更必须改变指纹"
    return True


def r2_plan_mismatch_named() -> bool:
    """执行参数与预注册不一致 → 拒绝，且错误信息点名不一致的字段。

    casekit 约定：FAILMSG 函数必须让异常自然上抛（本函数不 try/except），
    由 casekit 捕获后核对错误信息里的关键词。
    """
    from core.statistics.prereg import assert_plan_match

    plan = _make_plan()
    executed = {**plan, "method": "fisher"}  # 执行时换了方法 → 必须抛 PlanMismatchError
    assert_plan_match(plan, executed)
    return True  # 不会走到这里


def r3_prereg_ledger_duplicate_rejected() -> bool:
    """预注册台账 append-only：同一 plan_id 二次登记必须拒绝覆盖。

    异常自然向上抛（PreregLedgerError），由 casekit 判定。
    """
    from core.statistics.prereg import PreRegistrationLedger

    ledger = PreRegistrationLedger(_tmpdir() / "prereg.json")
    ledger.register(_make_plan())
    ledger.register(_make_plan())  # 重复 plan_id → 必须抛 PreregLedgerError
    return True


def r4_prereg_missing_blocked() -> bool:
    """论文轨执行必须携带已登记的 plan_id，缺失 → 显式拦截（PreregistrationRequired）。"""
    from core.statistics.prereg import PreRegistrationLedger
    from plugins.research.prereg_hook import require_preregistered

    ledger = PreRegistrationLedger(_tmpdir() / "prereg.json")  # 空台账
    require_preregistered(ledger, "pre-nonexistent", _make_plan())
    return True


# ---------------------------------------------------------------- R5-R8 数据集四要素 + IRB

def r5_dataset_gate_ok() -> bool:
    """四要素齐全 + IRB 批准未过期 → 通过。"""
    from plugins.research.dataset_gate import check_dataset_gate

    res = check_dataset_gate(_manifest())
    assert res["passed"] is True, f"合法清单应通过：{res}"
    assert res["dataset_name"] == "cohort-2026"
    return True


def r6_irb_expired_rejected() -> bool:
    """IRB 已过期 → 拒绝（论文轨不可用过期伦理审批跑数据）。"""
    from plugins.research.dataset_gate import check_dataset_gate

    check_dataset_gate(_manifest(expires="2020-01-01"))
    return True


def r7_dataset_missing_element_named() -> bool:
    """缺四要素之一 → 拒绝，且错误信息点名缺失要素。

    casekit 约定：FAILMSG 函数必须让异常自然上抛（本函数不 try/except），
    由 casekit 捕获后核对错误信息里的关键词。
    """
    from plugins.research.dataset_gate import check_dataset_gate

    check_dataset_gate(_manifest(missing="owner"))
    return True  # 不会走到这里


def r8_irb_not_approved_rejected() -> bool:
    """IRB 存在但未批准 → 拒绝（approved=False 与过期同属不可用）。"""
    from plugins.research.dataset_gate import check_dataset_gate

    check_dataset_gate(_manifest(approved=False))
    return True


# ---------------------------------------------------------------- R9-R11 k-匿名

def r9_k_anon_satisfied() -> bool:
    """k=2 下每个准标识符组合组大小 ≥2 → 满足。"""
    import duckdb

    from core.privacy.kanonymity import check_k_anonymity

    conn = duckdb.connect()
    try:
        conn.execute("CREATE TABLE cohort (participant_id VARCHAR, arm VARCHAR, "
                     "converted BOOLEAN, city VARCHAR, age INTEGER)")
        rows = [("p01", "a", True, "北京", 30), ("p02", "a", False, "北京", 30),
                ("p03", "b", True, "上海", 25), ("p04", "b", False, "上海", 25)]
        conn.executemany("INSERT INTO cohort VALUES (?,?,?,?,?)", rows)
        rep = check_k_anonymity(conn, "cohort", ["city", "age"], k=2)
        assert rep["satisfied"] is True, f"应满足 k=2：{rep}"
        assert rep["min_group_size"] >= 2
        return True
    finally:
        conn.close()


def r10_k_anon_violation_rejected() -> bool:
    """出现单例组（组大小 < k）→ 显式拒绝（PrivacyViolation 自然上抛）。"""
    import duckdb

    from core.privacy.kanonymity import assert_k_anonymity

    conn = duckdb.connect()
    try:
        conn.execute("CREATE TABLE cohort (participant_id VARCHAR, arm VARCHAR, "
                     "converted BOOLEAN, city VARCHAR, age INTEGER)")
        rows = [("p01", "a", True, "北京", 30), ("p02", "a", False, "北京", 30),
                ("p03", "b", True, "上海", 25)]  # 上海/25 是单例
        conn.executemany("INSERT INTO cohort VALUES (?,?,?,?,?)", rows)
        assert_k_anonymity(conn, "cohort", ["city", "age"], k=2)
        return True  # 不会走到这里
    finally:
        conn.close()


def r11_quasi_identifier_safety() -> bool:
    """准标识符列名必须是安全标识符：注入形态的列名 → ValueError（R9 语义安全）。"""
    import duckdb

    from core.privacy.kanonymity import check_k_anonymity

    conn = duckdb.connect()
    try:
        conn.execute("CREATE TABLE cohort (participant_id VARCHAR, city VARCHAR, age INTEGER)")
        conn.execute("INSERT INTO cohort VALUES ('p01','北京',30)")
        check_k_anonymity(conn, "cohort", ["city; DROP TABLE cohort"], k=2)
        return True  # 不会走到这里
    finally:
        conn.close()


# ---------------------------------------------------------------- R12-R14 DP 预算账本

def r12_dp_budget_spend_remaining() -> bool:
    """DP 预算记账：总预算 1.0，花 0.5 → 结余 0.5；spent 累加正确。"""
    from core.privacy.dp_budget import DpBudgetLedger

    ledger = DpBudgetLedger(_tmpdir() / "dp.json", total_epsilon=1.0)
    ledger.spend("spend-1", epsilon=0.5, purpose="mean disclosure")
    assert abs(ledger.remaining() - 0.5) < 1e-9, f"结余应为 0.5：{ledger.remaining()}"
    assert abs(ledger.spent() - 0.5) < 1e-9
    return True


def r13_dp_budget_exhausted_rejected() -> bool:
    """超预算支出 → PrivacyBudgetExhausted 自然上抛（被拒支出不得入账）。"""
    from core.privacy.dp_budget import DpBudgetLedger

    ledger = DpBudgetLedger(_tmpdir() / "dp.json", total_epsilon=1.0)
    ledger.spend("spend-1", epsilon=0.8, purpose="a")
    ledger.spend("spend-2", epsilon=0.5, purpose="b")  # 0.8+0.5 > 1.0
    return True  # 不会走到这里


def r14_dp_budget_append_only() -> bool:
    """DP 预算账本 append-only：重复 spend_id → DpLedgerError 自然上抛。"""
    from core.privacy.dp_budget import DpBudgetLedger

    ledger = DpBudgetLedger(_tmpdir() / "dp.json", total_epsilon=1.0)
    ledger.spend("spend-1", epsilon=0.5, purpose="a")
    ledger.spend("spend-1", epsilon=0.1, purpose="overwrite attempt")
    return True  # 不会走到这里


# ---------------------------------------------------------------- R15-R17 复现包

def _run_ledger(run_id: str = "r-repro-test") -> dict:
    return {
        "run_id": run_id,
        "track": "research",
        "started_at": "2026-08-31T00:00:00",
        "batch_id": "batch-repro-test",
        "snapshot_id": "s-repro-test",
        "snapshot_sha256": "0" * 64,
        "metric": "conversion_rate",
        "metric_version": 1,
        "health_summary": {"rows": 4},
        "provenance": {
            "run_id": run_id,
            "snapshot_id": "s-repro-test",
            "batch_id": "batch-repro-test",
            "commit_sha": "0" * 64,
        },
        "steps": ["gate", "analyze"],
    }


def r15_repro_build_verify() -> bool:
    """复现包构建 → verify 全匹配（raw/snapshot sha 重算一致、manifest 完整）。"""
    from core.repro.package import build_repro_package, verify_repro_package

    root = _tmpdir()
    raw = _cohort_csv(root / "cohort.csv", [
        ("p01", "a", "1", "北京", "30"), ("p02", "a", "0", "北京", "30"),
        ("p03", "b", "1", "上海", "25"), ("p04", "b", "0", "上海", "25")])
    snapshot_db = root / "snapshot" / "research.db"
    snapshot_db.parent.mkdir(parents=True)
    snapshot_db.write_bytes(b"\x00" * 256)
    prereg = root / "prereg.json"
    prereg.write_text(json.dumps({"plan_id": "pre-test-001"}), encoding="utf-8")
    manifest = root / "dataset.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")

    info = build_repro_package(_run_ledger(), root / "volume", root / "out",
                               raw, snapshot_db, prereg, manifest)
    assert info["package_dir"].exists(), "复现包目录必须存在"
    assert info["zip_path"].exists(), "复现包 zip 必须存在"
    assert (info["package_dir"] / "code_digest.txt").exists(), "必须有代码指纹"

    rep = verify_repro_package(info["package_dir"])
    assert rep["ok"] is True, f"未篡改的复现包必须通过校验：{rep}"
    assert rep["raw_sha256_match"] is True and rep["snapshot_sha256_match"] is True
    return True


def r16_repro_tamper_detected() -> bool:
    """篡改 raw → verify 必须诚实报告不匹配（不得伪装通过）。"""
    from core.repro.package import build_repro_package, verify_repro_package

    root = _tmpdir()
    raw = _cohort_csv(root / "cohort.csv", [("p01", "a", "1", "北京", "30")])
    snapshot_db = root / "snapshot" / "research.db"
    snapshot_db.parent.mkdir(parents=True)
    snapshot_db.write_bytes(b"\x00" * 128)
    prereg = root / "prereg.json"
    prereg.write_text(json.dumps({"plan_id": "pre-test-001"}), encoding="utf-8")
    manifest = root / "dataset.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")

    info = build_repro_package(_run_ledger(), root / "volume", root / "out",
                               raw, snapshot_db, prereg, manifest)
    (info["package_dir"] / "raw" / "cohort.csv").write_text(
        "participant_id,arm,converted,city,age\np99,b,1,广州,99\n", encoding="utf-8")
    rep = verify_repro_package(info["package_dir"])
    assert rep["ok"] is False, "篡改后必须报告不匹配"
    assert rep["raw_sha256_match"] is False, "必须点名 raw 不匹配"
    return True


def r17_repro_missing_provenance_rejected() -> bool:
    """run 台账缺溯源四元组 → 构建复现包直接拒绝（ReproError 自然上抛，D3 不可缺）。"""
    from core.repro.package import build_repro_package

    root = _tmpdir()
    raw = _cohort_csv(root / "cohort.csv", [("p01", "a", "1", "北京", "30")])
    snapshot_db = root / "snapshot" / "research.db"
    snapshot_db.parent.mkdir(parents=True)
    snapshot_db.write_bytes(b"\x00" * 128)
    prereg = root / "prereg.json"
    prereg.write_text(json.dumps({"plan_id": "pre-test-001"}), encoding="utf-8")
    manifest = root / "dataset.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")

    bad_ledger = _run_ledger()
    bad_ledger["provenance"] = {"run_id": "r-repro-test"}  # 缺 snapshot/batch/commit
    build_repro_package(bad_ledger, root / "volume", root / "out",
                        raw, snapshot_db, prereg, manifest)
    return True  # 不会走到这里


# ---------------------------------------------------------------- R18-R19 统计套件诚实边界

def r18_count_family_explicit_failure() -> bool:
    """count 族中**未实现**的方法（poisson_regression）→ 显式 MethodContractError。

    登记册有、执行器没有 → 必须点名拒绝，绝不回落到已实现方法（不静默降级）。
    说明：negative_binomial 执行器已落地（S14），此处以 poisson_regression
    （count×observational 候选族）作为「登记册有 / 执行器未实现」的活体样本。
    """
    analyze("count", "observational", "research", counts=(10, 100, 12, 100),
            method="poisson_regression", mde=0.1)
    return True  # 不会走到这里


def r19_research_mode_diagnosis_completeness() -> bool:
    """research_mode=True 的 RED 诊断书追加识别假设清单与安慰剂检验模板（§09.1）。"""
    c_on = analyze("binary", "two_group", "research", counts=(7, 8, 5, 8),
                   mde=0.05, research_mode=True)
    c_off = analyze("binary", "two_group", "research", counts=(7, 8, 5, 8),
                    mde=0.05, research_mode=False)
    assert c_on["status"] == "RED" and c_off["status"] == "RED"
    d_on = c_on["method"]["diagnosis"]
    d_off = c_off["method"]["diagnosis"]
    assert d_on["identification_assumptions"], "research_mode 必须给出识别假设清单"
    assert d_on["placebo_tests"], "research_mode 必须给出安慰剂检验模板"
    assert not d_off["identification_assumptions"], "非 research_mode 不追加研究假设"
    return True


# ---------------------------------------------------------------- R20-R21 Quarto + 端到端

def r20_quarto_render_r_file() -> bool:
    """Quarto 出版：产出 [R] 前缀 .qmd，结论数字走 {…} 插值（不硬编码）。"""
    from plugins.research.quarto import render_research_qmd

    c = analyze("binary", "rct", "research", counts=(460, 1000, 500, 1000),
                mde=0.08, question="cohort conversion", period="2026-08",
                research_mode=True)
    out = render_research_qmd(c, plan=_make_plan(), out_dir=_tmpdir())
    assert out.exists() and out.name.startswith("[R]"), f"必须产出 [R] 文件：{out.name}"
    text = out.read_text(encoding="utf-8")
    assert "{effect" in text or "{p_value" in text, "结论数字必须以插值出现"
    return True


def r21_research_pipeline_e2e() -> bool:
    """论文轨端到端：raw→体检→数据集 gate→预注册 gate→断言→k-匿名→DP→组计数→
    analyze→claims 对账→快照→台账（四元组）→复现包→Quarto 全链路闭合。

    - 计数由管道从数据推导（调用方不喂 counts——诚实边界）；
    - 数据量满足 mde=0.08 的样本量 → GREEN → claims 对账真实执行（C-5）；
    - 台账 SQLite 可机查；快照目录与复现包真实存在。
    """
    import sqlite3

    from core.ingestion.context import TrackContext
    from core.statistics.prereg import PreRegistrationLedger
    from plugins.research.run_research import run_research_pipeline

    root = _tmpdir()
    vol = root / "volume"

    # 每臂 650 行：a 臂 325 转化（i%2==0），b 臂 217 转化（i%3==0）；
    # city/age 均匀分布于 3 城 × 2 龄 → 每个准标识符组合远超 k=2。
    cities, ages = ["北京", "上海", "广州"], [25, 35]
    rows = []
    for i in range(650):
        rows.append((f"p{i + 1:04d}", "a", 1 if i % 2 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    for i in range(650):
        rows.append((f"p{i + 651:04d}", "b", 1 if i % 3 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    raw = _cohort_csv(root / "cohort.csv", rows)

    manifest_path = root / "dataset.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    plan = _make_plan()  # mde=0.08, alpha=0.05, power=0.8, method=two_proportion_test
    prereg_path = root / "prereg.json"
    PreRegistrationLedger(prereg_path).register(plan)
    dp_path = root / "dp.json"

    ctx = TrackContext(track="research", volume_root=vol)
    out = run_research_pipeline(
        ctx=ctx, raw_csv=raw, manifest_path=manifest_path,
        prereg_path=prereg_path, prereg_id=plan["plan_id"], plan=plan,
        outcome="binary", design="rct", method="two_proportion_test",
        question="arm conversion", period="2026-08", mde=0.08,
        dp_budget_path=dp_path, dp_epsilon=0.1,
    )
    run_id = out["run_id"]
    assert out["snapshot_id"] and out["conclusion"]["status"], "快照与结论必须产出"
    assert out["conclusion"]["status"] == "GREEN", (
        f"样本量满足 mde=0.08 应为 GREEN，实为 {out['conclusion']['status']}")
    assert out["reconcile"]["reconcile_diff"] == "clean", "claims 对账必须 clean"
    assert out["counts"] == (325, 650, 217, 650), f"计数必须来自数据：{out['counts']}"
    assert out["dp_remaining"] is not None and abs(out["dp_remaining"] - 0.9) < 1e-9, (
        "DP 预算 spend 0.1 后剩余应为 0.9")

    # 台账可机查：runs 表存在该 run，track=research，快照与批次溯源齐全
    conn = sqlite3.connect(str(out["ledger_db"]))
    try:
        row = conn.execute(
            "SELECT track, snapshot_id, batch_id, provenance FROM runs WHERE run_id=?",
            (run_id,)).fetchone()
        events = conn.execute(
            "SELECT event_type FROM events WHERE subject_id=? AND event_type='prereg_checked'",
            (run_id,)).fetchall()
    finally:
        conn.close()
    assert row is not None, f"台账缺少 run {run_id}"
    assert row[0] == "research"
    prov = json.loads(row[3])
    for k in ("run_id", "snapshot_id", "batch_id", "commit_sha"):
        assert prov.get(k), f"台账溯源缺 {k}"
    assert events, "预注册检查必须进 append-only 事件流"

    # 快照目录与复现包必须真实存在
    snap_dir = ctx.snapshots_dir / out["snapshot_id"]
    assert snap_dir.exists(), "快照目录必须存在"
    assert out["repro_package"] and Path(out["repro_package"]).exists(), "复现包必须存在"
    assert out["qmd_path"] and Path(out["qmd_path"]).name.startswith("[R]"), "Quarto 出版缺失"
    return True


def r22_research_modeling_via_dbt() -> bool:
    """论文轨建模必须由 dbt 物化，并显式 --select dwd_cohort（防双轨模型串门）。

    双轨共用同一 dbt 工程：论文轨若落回默认 select（dwd_base），就会把 commerce
    的模型物化进 research 库——属模型串门。本用例以 spy 记录真实调用参数（包住原
    函数、仍真跑 dbt，不做替代），并校验建模从不可变批次读（P3：不读上传临时文件）。
    """
    from core.ingestion.context import TrackContext
    from core.modeling import dbt as dbt_mod
    from core.statistics.prereg import PreRegistrationLedger
    import plugins.research.run_research as rr

    from plugins.research.run_research import run_research_pipeline

    root = _tmpdir()
    vol = root / "volume"
    cities, ages = ["北京", "上海", "广州"], [25, 35]
    rows = []
    for i in range(60):
        rows.append((f"p{i + 1:04d}", "a", 1 if i % 2 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    for i in range(60):
        rows.append((f"p{i + 61:04d}", "b", 1 if i % 3 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    raw = _cohort_csv(root / "cohort.csv", rows)

    # 清单不带 privacy 块 → 跳过 k-匿名 gate（本用例只测建模接线）
    man = {"dataset_name": "m", "version": "1.0.0", "owner": "o",
           "collection_method": "c",
           "irb": {"approved": True, "expires_at": "2099-12-31",
                   "approval_no": "IRB-X"}}
    manifest_path = root / "dataset.json"
    manifest_path.write_text(json.dumps(man), encoding="utf-8")

    plan = _make_plan()
    prereg_path = root / "prereg.json"
    PreRegistrationLedger(prereg_path).register(plan)

    calls: list[dict] = []
    orig = dbt_mod.run_dbt_model

    def spy(db_path, raw_csv, select="dwd_base"):
        calls.append({"select": select, "raw_csv": str(raw_csv)})
        return orig(db_path, raw_csv, select=select)   # 真跑 dbt，只记录不替代

    rr.run_dbt_model = spy
    try:
        ctx = TrackContext(track="research", volume_root=vol)
        out = run_research_pipeline(
            ctx=ctx, raw_csv=raw, manifest_path=manifest_path,
            prereg_path=prereg_path, prereg_id=plan["plan_id"], plan=plan,
            outcome="binary", design="rct", method="two_proportion_test",
            question="q", period="2026-08", mde=0.08,
        )
    finally:
        rr.run_dbt_model = orig

    assert calls, "论文轨建模必须走 dbt 声明式建模（run_dbt_model 未被调用）"
    assert calls[0]["select"] == "dwd_cohort", (
        f"论文轨必须显式 --select dwd_cohort，实为 {calls[0]['select']}"
        "（落回默认 dwd_base 属模型串门）")
    raw_used = calls[0]["raw_csv"]
    assert "raw" in raw_used or Path(raw_used).name.startswith("b20"), (
        f"建模必须从不可变批次读（P3），实为 {raw_used}")
    assert out["run_id"] and out["snapshot_id"], "dbt 建模后管道仍须闭合"
    return True


def _mini_pipeline(root: Path):
    """跑一次精简论文轨管道（60+60，无 privacy 块），返回 (out, vol)。"""
    from core.ingestion.context import TrackContext
    from core.statistics.prereg import PreRegistrationLedger
    from plugins.research.run_research import run_research_pipeline

    vol = root / "volume"
    cities, ages = ["北京", "上海", "广州"], [25, 35]
    rows = []
    for i in range(60):
        rows.append((f"p{i + 1:04d}", "a", 1 if i % 2 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    for i in range(60):
        rows.append((f"p{i + 61:04d}", "b", 1 if i % 3 == 0 else 0,
                     cities[i % 3], ages[(i // 3) % 2]))
    raw = _cohort_csv(root / "cohort.csv", rows)
    man = {"dataset_name": "m", "version": "1.0.0", "owner": "o",
           "collection_method": "c",
           "irb": {"approved": True, "expires_at": "2099-12-31",
                   "approval_no": "IRB-X"}}
    manifest_path = root / "dataset.json"
    manifest_path.write_text(json.dumps(man), encoding="utf-8")
    plan = _make_plan()
    prereg_path = root / "prereg.json"
    PreRegistrationLedger(prereg_path).register(plan)
    ctx = TrackContext(track="research", volume_root=vol)
    out = run_research_pipeline(
        ctx=ctx, raw_csv=raw, manifest_path=manifest_path,
        prereg_path=prereg_path, prereg_id=plan["plan_id"], plan=plan,
        outcome="binary", design="rct", method="two_proportion_test",
        question="q", period="2026-08", mde=0.08,
    )
    return out, vol


def r23_method_registry_freeze_switch() -> bool:
    """research-methods 登记册冻结开关（S3 清单项）：
    未冻结可注册；冻结后任何改动抛 FrozenRegistryError；取族返回副本（改返回值不污染登记册）。
    """
    from core.statistics.method_contract import (
        FrozenRegistryError,
        MethodRegistry,
        candidate_families,
        register_family,
    )

    reg = MethodRegistry()
    assert not reg.frozen, "新登记册默认未冻结"
    # ① 取族返回副本：外部改动不得污染登记册（否则冻结形同虚设）
    fam = candidate_families("binary", "rct", registry=reg)
    fam.append("injected_method")
    assert "injected_method" not in candidate_families("binary", "rct", registry=reg), (
        "candidate_families 必须返回副本")

    # ② 未冻结时可注册
    register_family("count", "rct", ["poisson_test", "poisson_regression"],
                    registry=reg, replace=True)
    assert "poisson_regression" in candidate_families("count", "rct", registry=reg)

    # ③ 冻结后改动被拒
    reg.freeze()
    assert reg.frozen
    try:
        register_family("count", "rct", ["poisson_test"], registry=reg, replace=True)
        return False  # 冻结后注册必须被拒
    except FrozenRegistryError as e:
        assert "冻结" in str(e), "错误消息必须点名冻结"
    return True


def r24_registry_fingerprint_stable() -> bool:
    """登记册指纹：同内容一致、内容变则变（供台账溯源与复现包核验方法集）。"""
    from core.statistics.method_contract import (
        MethodRegistry,
        register_family,
        registry_fingerprint,
    )

    a, b = MethodRegistry(), MethodRegistry()
    assert registry_fingerprint(a) == registry_fingerprint(b), "同内容登记册指纹必须一致"
    c = MethodRegistry()
    # 登记册原本 ("count","rct") = ["poisson_test"] → 换内容后指纹必须变
    register_family("count", "rct", ["poisson_test", "negative_binomial"],
                    registry=c, replace=True)
    assert registry_fingerprint(c) != registry_fingerprint(a), "内容变化指纹必须变化"
    assert len(registry_fingerprint(a)) == 64, "指纹应为 sha256 hex"
    return True


def r25_run_records_methods_fingerprint() -> bool:
    """论文轨 run：本次 run 使用**冻结**的方法登记册，指纹记入台账 provenance。

    语义（架构书 S3「登记册 + 冻结开关」）：预注册 gate 时刻冻结方法集，
    分析期间方法集不可变——防止「边跑边改方法」这种事后合理化。
    """
    root = _tmpdir()
    out, vol = _mini_pipeline(root)
    ledger_json = json.loads((vol / "runs" / f"{out['run_id']}.json").read_text(
        encoding="utf-8"))
    prov = ledger_json.get("provenance") or {}
    fp = prov.get("methods_fingerprint")
    assert fp and len(fp) == 64, f"台账必须记录方法集指纹，实为 {fp!r}"
    # 与当前默认登记册指纹一致（本次 run 未改动方法集）
    from core.statistics.method_contract import registry_fingerprint

    assert fp == registry_fingerprint(), "run 方法集指纹应等于当前默认登记册指纹"
    return True


def r26_register_on_frozen_registry_rejected() -> bool:
    """冻结后注册新族 → FrozenRegistryError（拒绝而非静默）。"""
    from core.statistics.method_contract import MethodRegistry, register_family

    reg = MethodRegistry()
    reg.freeze()
    register_family("count", "rct", ["poisson_test"], registry=reg)  # 必须抛
    return True


def r27_poisson_rate_ratio_test() -> bool:
    """count 族 poisson_test：率比 RR + 条件二项精确 CI，过离散如实标注未检。

    counts 口径（count 结局）= (events_a, exposure_a, events_b, exposure_b)：
    exposure 是暴露量（人天/曝光次数），事件数可以超过它——与二分类的 n 不是一回事。
    """
    c = analyze("count", "two_group", "research", counts=(120, 1000, 100, 1000),
                method="poisson_test", mde=0.1, suite="full")
    assert c["method"]["registered_name"] == "poisson_test", (
        f"方法名应取登记册口径，实为 {c['method']['registered_name']}")
    r = c["result"]
    assert r["effect"]["type"] == "rate_ratio", "count 结局效应量必须是率比 RR"
    assert abs(r["effect"]["value"] - 1.2) < 1e-9, (
        f"RR 应为 (120/1000)/(100/1000)=1.2，实为 {r['effect']['value']}")
    lo, hi = r["effect"]["ci95"]
    assert lo < 1.2 < hi, f"CI 必须覆盖点估计，实为 [{lo}, {hi}]"
    assert 0.0 < r["p_value"] < 1.0, "p 值必须在 (0,1)"
    # D5：过离散未检 → 必须如实标注，绝不伪装成"已通过"
    blob = json.dumps(c, ensure_ascii=False, default=str)
    assert "过离散" in blob, "过离散（overdispersion）未检必须如实标注"
    return True


def r28_poisson_input_rejected() -> bool:
    """poisson_test 输入非法 → 显式拒绝（暴露量为 0 时率无定义），不静默。"""
    from core.statistics.executor import poisson_test

    poisson_test(0, 0, 5, 100)   # 暴露量 n_a=0 → 率无定义，必须抛
    return True  # 不会走到这里


def r29_count_nb_analyze() -> bool:
    """count 族 negative_binomial 执行器接入 analyze（登记册有 → 执行器必须有对应物）。

    count×two_group 候选族 = [poisson_test, negative_binomial]：两者都必须能跑，
    不能只留登记册名字而执行器缺位（S3 余项⑫ 整改：count 族不再半成品）。
    NB2（α=1）方差结构已吸收过离散 → 结论不再重复报「过离散未检」（与 poisson 不同）。
    """
    c = analyze("count", "two_group", "research", counts=(120, 1000, 100, 1000),
                method="negative_binomial", mde=0.1, suite="full")
    assert c["method"]["registered_name"] == "negative_binomial", (
        f"方法名应取登记册口径，实为 {c['method']['registered_name']}")
    assert c["method"]["candidate_families"] == ["poisson_test", "negative_binomial"], (
        "候选族必须完整列出 count 族两个方法")
    r = c["result"]
    assert r is not None, "negative_binomial 结论必须产出 result（不能是半成品）"
    assert r["effect"]["type"] == "rate_ratio", "count 结局效应量必须是率比 RR"
    assert 0.0 < r["p_value"] < 1.0, "p 值必须在 (0,1)"
    blob = json.dumps(r, ensure_ascii=False, default=str)
    assert "rate_ratio" in blob
    unchecked = r.get("unchecked_items") or []
    assert "过离散" not in unchecked, (
        "NB2 已建模过离散，未检项清单不应再报「过离散未检」")
    return True


def r30_count_replay_reconcile() -> bool:
    """count 族结论的 claims 对账基准来自**重算**（C-5）：replay_stat 必须覆盖
    poisson_test / negative_binomial——否则论文轨用 count 方法时对账基准为空，
    所有 claim 会被判 FAIL（reconcile_diff=MISMATCH → 报告不渲染），即静默断链。

    S3 余项⑫ 整改前的实际状态：replay_stat 对 count 方法落 `else: return {}`——
    本用例在旧实现上必然 RED（reconcile_diff != clean）。
    """
    from core.claims.reconcile import reconcile
    from core.statistics.executor import analyze, replay_stat

    for method, counts in (
        ("poisson_test", (120, 1000, 100, 1000)),
        ("negative_binomial", (120, 1000, 100, 1000)),
    ):
        source = {"query_id": f"q-{method}", "run_id": f"r-{method}",
                  "snapshot_id": f"s-{method}"}
        c = analyze("count", "two_group", "research", counts=counts,
                    method=method, mde=0.1, period="2026-08", source=source,
                    suite="full")
        assert c["result"] is not None, f"{method} 应产出结论"
        replay = replay_stat(c, counts=counts)
        assert replay, f"{method} 的重放基准不得为空（replay_stat 必须覆盖 count 族）"
        rec = reconcile(c["claims"], replay)
        assert rec["reconcile_diff"] == "clean", (
            f"{method} claims 对账必须 clean，实为 {rec['reconcile_diff']}: "
            f"{[f['reason'] for f in rec['failed']]}")
    return True


# ---------------------------------------------------------------- 声明式用例表

CASES: tuple = (
    ("R1 预注册冻结：计划指纹稳定", r1_plan_fingerprint_stable),
    ("R5 数据集四要素+IRB 校验通过", r5_dataset_gate_ok),
    ("R9 k-匿名满足 k=2", r9_k_anon_satisfied),
    ("R12 DP 预算记账与结余", r12_dp_budget_spend_remaining),
    ("R15 复现包构建并通过校验", r15_repro_build_verify),
    ("R16 复现包篡改被诚实检出", r16_repro_tamper_detected),
    ("R19 research_mode 诊断书完备度", r19_research_mode_diagnosis_completeness),
    ("R20 Quarto 出版 [R] 文件", r20_quarto_render_r_file),
    ("R21 论文轨端到端闭环", r21_research_pipeline_e2e),
    ("R22 论文轨建模走 dbt --select dwd_cohort", r22_research_modeling_via_dbt),
    ("R23 方法登记册冻结开关", r23_method_registry_freeze_switch),
    ("R24 登记册指纹稳定/变化可辨", r24_registry_fingerprint_stable),
    ("R25 run 台账记录方法集指纹（冻结）", r25_run_records_methods_fingerprint),
    ("R27 count 族 poisson 率比检验", r27_poisson_rate_ratio_test),
    ("R29 count 族 negative_binomial 接入 analyze", r29_count_nb_analyze),
    ("R30 count 族 claims 对账基准来自重算", r30_count_replay_reconcile),
)

REJECT_CASES: tuple = (
    ("R3 预注册台账重复 plan_id 拒绝覆盖", "PreregLedgerError", r3_prereg_ledger_duplicate_rejected),
    ("R4 预注册缺失被拦截", "PreregistrationRequired", r4_prereg_missing_blocked),
    ("R6 IRB 过期被拒", "IrbExpired", r6_irb_expired_rejected),
    ("R8 IRB 未批准被拒", "DatasetGateError", r8_irb_not_approved_rejected),
    ("R10 未达 k-匿名被拒", "PrivacyViolation", r10_k_anon_violation_rejected),
    ("R11 非法准标识符列名被拒", "ValueError", r11_quasi_identifier_safety),
    ("R13 超 DP 预算被拒", "PrivacyBudgetExhausted", r13_dp_budget_exhausted_rejected),
    ("R14 DP 账本重复 spend_id 拒绝覆盖", "DpLedgerError", r14_dp_budget_append_only),
    ("R17 缺溯源四元组拒绝构建复现包", "ReproError", r17_repro_missing_provenance_rejected),
    ("R18 count 族显式失败不静默", "MethodContractError", r18_count_family_explicit_failure),
    ("R26 冻结登记册拒绝注册", "FrozenRegistryError", r26_register_on_frozen_registry_rejected),
    ("R28 poisson 暴露量非法被拒", "StatInputError", r28_poisson_input_rejected),
)

FAILMSG_CASES: tuple = (
    ("R2 执行与预注册不一致被拒并点名字段", r2_plan_mismatch_named, "method"),
    ("R7 数据集缺要素被拒并点名要素", r7_dataset_missing_element_named, "owner"),
)


def _resolve(cases: tuple) -> tuple:
    """把 REJECT_CASES 的异常类型名解析为真实类。

    惰性 import：S3 模块未实现时返回兜底 RuntimeError，保证 pytest 收集不崩、
    用例保持独立 RED；实现后就地解析到真实异常类。
    """
    out = []
    for name, exc_name, fn in cases:
        if exc_name == "ValueError":
            exc_type = ValueError
        elif exc_name == "MethodContractError":
            exc_type = MethodContractError
        else:
            exc_type = _import_exc(exc_name)
        out.append((name, exc_type, fn))
    return tuple(out)


def _import_exc(exc_name: str):
    import importlib

    for mod in ("core.statistics.prereg", "core.statistics.method_contract",
                "core.statistics.executor",
                "plugins.research.prereg_hook",
                "plugins.research.dataset_gate", "core.privacy.kanonymity",
                "core.privacy.dp_budget", "core.repro.package"):
        try:
            m = importlib.import_module(mod)
        except Exception:  # noqa: BLE001 — 模块未实现时保持 RED 而非收集崩溃
            continue
        if hasattr(m, exc_name):
            return getattr(m, exc_name)
    return RuntimeError  # 模块未实现 → 用例以 RuntimeError 兜底仍可执行（RED 期）


if __name__ == "__main__":
    sys.exit(casekit.run_cli("R 论文轨测试（S3）",
                             CASES, _resolve(REJECT_CASES), FAILMSG_CASES))

test_pass, test_reject, test_failmsg = casekit.pytest_cases(
    CASES, _resolve(REJECT_CASES), FAILMSG_CASES)
