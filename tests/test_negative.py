#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V1 负面测试（架构书附录 T 规格）—— CI 永久工件。

十六条用例（12 拒 + 4 必过）：正确的系统必须「拒绝」，拒绝失败 = 测试失败。
  N1-N12  见架构书附录 T 施工规格映射表
  N13     ADR-18 下游：对账未过的报告必须拒绝渲染（P4 禁止静默降级）
  N14     R7 门禁的元测试：确保「模板硬编码数字」规则自身没有失效
  N15     per-key 白名单轨道绑定（审计 #8）：key 只被授权 commerce，却在
          research 上调用 → 拒绝 + 审计
  N16     per-key 白名单轨道绑定正例：同一把 key 在本轨调用 → 放行
运行：仓库根目录下  python tests/test_negative.py
临时契约只写入系统临时目录或仓库内已忽略的测试目录，不污染正式契约目录。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 宿主 python 不跨进程继承 PYTHONPATH（README 已记录）
sys.path.insert(0, str(Path(__file__).resolve().parent))  # P2-01：稳定 import casekit

import yaml

import casekit
from core.audit.ledger import LedgerError, write_run
from core.auth.tool_registry import PermissionDenied, ToolRegistry
from core.claims.reconcile import ClaimError, reconcile
from core.ingestion.context import TrackContext
from core.report.kernel import ReportBlocked
from core.semantic.api import SemanticQueryError
from core.semantic.compiler import ContractError, load_contract


def n1_cross_track_escape() -> None:
    commerce = TrackContext(track="commerce", volume_root=ROOT / "data" / "commerce")
    commerce.assert_inside_volume(ROOT / "data" / "research" / "raw" / "x.db")


def _load_bad_contract(fields: dict) -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad_contract.yml"
        p.write_text(yaml.safe_dump(fields), encoding="utf-8")
        load_contract(p)  # 期望抛 ContractError


def n2_missing_grain() -> None:
    _load_bad_contract({
        "metric": "x", "type": "ratio",
        "numerator": {"metric": "a"}, "denominator": {"metric": "b"},
        "time_column": "t",
    })


def n3_missing_time_column() -> None:
    _load_bad_contract({
        "metric": "x", "type": "ratio",
        "numerator": {"metric": "a"}, "denominator": {"metric": "b"},
        "grain": "order",
    })


def n4_invalid_track() -> None:
    TrackContext(track="research2", volume_root=ROOT / "data" / "research")


def n5_unknown_metric_query() -> None:
    """Semantic API：未知/未编译指标 → 拒绝（负向，附录 T「Semantic API」行）。"""
    import duckdb

    from core.semantic import api
    from core.semantic.compiler import compile_metric

    contract_path = ROOT / "schemas" / "metrics" / "commerce" / "pay_success_rate.yml"
    conn = duckdb.connect()
    try:
        conn.execute(
            "CREATE TABLE dwd_base (created_at TIMESTAMP, amount_total DOUBLE, "
            "amount_paid DOUBLE, status VARCHAR)"
        )  # 编译视图所需的基础表
        contract = load_contract(contract_path)
        # 嵌套契约编译（numerator/denominator 引用子契约视图，需 contracts_dir）
        compile_metric(conn, "commerce", contract, contracts_dir=contract_path.parent)
        # 请求一个从未编译的指标名 → 视图不存在 → SemanticQueryError
        fake_dir = ROOT / "_test_tmp_n5"
        fake_dir.mkdir(exist_ok=True)
        fake = fake_dir / "_n5_fake.yml"
        fake.write_text(yaml.safe_dump({**contract, "metric": "nonexistent_metric"}),
                        encoding="utf-8")
        try:
            api.query_metric(conn, ROOT / "data" / "runs" / "ledger.db",
                             fake, "commerce")
        finally:
            # 清理必须吞掉环境层异常：本机 safe-delete shim 在删除配额
            # 超限时会把 unlink 变成 SystemExit（Windows ACL 场景同理）。
            # 清理失败时文件只会留在 .gitignore 明确排除的测试目录。
            try:
                fake.unlink(missing_ok=True)
            except SystemExit as e:  # noqa: BLE001 — 环境守护拦截，非断言失败
                print(f"[n5] 清理被环境守护拦截（遗留 {fake.name}）：{e}",
                      file=sys.stderr)
    finally:
        conn.close()


def n6_tampered_claim_fails() -> bool:
    """Claim Schema：人为改 claim.value → reconcile FAIL 且报告不渲染（必须被识破）。"""
    claims = [{
        "metric": "pay_success_rate", "period": "2026-08-26", "operation": "point",
        "value": 0.9999,  # 篡改：真实值 0.875（订单数口径，见 N12）
        "unit": "ratio",
        "source": {"query_id": "q-1", "run_id": "r-1", "snapshot_id": "s-1"},
    }]
    out = reconcile(claims, {"2026-08-26": 0.875})
    return out["reconcile_diff"] == "MISMATCH" and out["quality_gate"] == "blocked"


def n7_claim_missing_source_rejected() -> None:
    """Claim Schema：source 三元组缺失 → 校验拒绝（负向，附录 T「Claim Schema」行）。"""
    reconcile([{
        "metric": "x", "period": "2026-08-26", "value": 1.0, "unit": "ratio",
        "source": {"query_id": "q-1"},  # 缺 run_id / snapshot_id
    }], {})


def n8_ledger_missing_provenance_rejected() -> None:
    """Run/Snapshot/Provenance：结论对象缺 provenance 任一元 → 入库被拒。"""
    with tempfile.TemporaryDirectory() as td:
        write_run(Path(td) / "ledger.db", {
            "run_id": "r-x",
            "track": "commerce",
            "started_at": "2026-08-28T00:00:00",
            "metric": "m",
            "provenance": {"snapshot_id": "s-x"},  # 缺 run_id / batch_id
        })


def n9_l2_out_of_whitelist_denied() -> None:
    """Capability Matrix：L2 key 尝试白名单外动作 → 拒绝 + 审计。"""
    audit: list = []
    reg = ToolRegistry(audit_sink=audit)
    try:
        reg.authorize("L2", "drop_schema", "commerce")
    finally:
        assert any("DENIED" in c.audit_note for c in audit), "越界动作必须落审计"


def n10_proportion_diff_no_welch() -> bool:
    """统计方法契约：比例差异问题不再产出 welch_t（附录 T 回归用例）。"""
    from core.statistics.method_contract import candidate_families

    fams = candidate_families("binary", "two_group")
    return "welch_t_test" not in fams and "two_proportion_test" in fams


def n11_per_key_whitelist_denied() -> None:
    """Capability Matrix（ADR-17 L2 / S2 判据）：per-key 白名单外动作 → 拒绝 + 审计。"""
    from core.auth.tool_registry import PermissionDenied, ToolRegistry, register_key

    register_key("key-com-001", {"read_via_semantic", "pipeline_rerun"},
                 tracks={"commerce"})  # L2 key，只授权商用轨
    audit: list = []
    reg = ToolRegistry(audit_sink=audit)
    try:
        reg.authorize("L2", "drop_schema", "commerce", key_id="key-com-001")
    finally:
        assert any("DENIED" in c.audit_note and c.key_id == "key-com-001" for c in audit), \
            "per-key 越界动作必须落审计且记录 key_id"


def n12_metric_order_count_regression() -> bool:
    """Metric Contract 口径回归：pay_success_rate 为订单数口径（架构书清单 4-A）。

    8 行演示数据（7 paid + 1 cancelled）：
    - 订单数口径 = pay_success_orders(7) / pay_attempted_orders(8) = 0.875
    - 若退回金额口径（507/595≈0.8521）→ 断言失败。
    """
    import duckdb as _duckdb
    import tempfile

    from core.semantic import api
    from core.semantic.compiler import compile_metric, load_contract

    contract_dir = ROOT / "schemas" / "metrics" / "commerce"
    contract_path = contract_dir / "pay_success_rate.yml"
    demo_csv = ROOT / "examples" / "测试数据-8月26日订单.csv"
    assert demo_csv.exists(), f"demo csv missing: {demo_csv}"

    conn = _duckdb.connect()
    try:
        # 演示数据有 status 列（7 paid + 1 cancelled）
        conn.execute(
            f"CREATE TABLE dwd_base AS SELECT * FROM read_csv('{str(demo_csv)}', "
            f"header=true, sample_size=-1, strict_mode=false)"
        )
        contract = load_contract(contract_path)
        compile_metric(conn, "commerce", contract, contracts_dir=contract_dir)
        with tempfile.TemporaryDirectory() as td:
            q = api.query_metric(conn, Path(td) / "ledger.db", contract_path, "commerce")
            rows = q["rows"]
        if not rows:
            return False
        value = rows[0][1]
        return abs(value - 7 / 8) < 1e-9 and abs(value - 0.8521) > 1e-6
    finally:
        conn.close()


def n13_report_blocked_when_claims_mismatch() -> None:
    """ADR-18：claims 对账不通过 → 结论对象拒绝生成（报告不得渲染）。

    这是 N6 的下游延伸：N6 证明篡改「会被识破」，本例证明识破之后
    确实拦在渲染管线之外，而不是渲染一份带警告的报告（P4：禁止静默降级）。
    """
    from core.report.kernel import ReportBlocked, build_conclusion

    tampered = reconcile(
        [{"metric": "pay_success_rate", "period": "2026-08-26", "operation": "point",
          "value": 0.9999, "unit": "ratio",
          "source": {"query_id": "q-1", "run_id": "r-1", "snapshot_id": "s-1"}}],
        {"2026-08-26": 0.875},
    )
    assert tampered["quality_gate"] == "blocked", "前置：篡改必须已被识破"
    build_conclusion(
        {"run_id": "r-1", "track": "commerce", "metric": "pay_success_rate",
         "metric_series": [["2026-08-26", 0.875]], "rows": 8,
         "provenance": {"run_id": "r-1", "snapshot_id": "s-1", "batch_id": "b-1"}},
        tampered,
    )


def n14_template_hardcoded_number_detected() -> bool:
    """ADR-18 / CI R7 元测试：模板里写死数字必须被门禁抓到。

    防止 R7 规则自身失效（正则写错、范围写窄）——门禁也要有门禁。
    """
    sys.path.insert(0, str(ROOT / "tests"))
    from check_dependencies import TEMPLATE_NUMBER_RE, _strip_placeholders

    # 插值表达式里的数字（如默认值）不该被判违规
    assert not TEMPLATE_NUMBER_RE.findall(_strip_placeholders("版本 v{x.get('v', 1)}")), \
        "插值内的数字被误判"
    # 写死的业务数字必须被抓到
    return bool(TEMPLATE_NUMBER_RE.findall(_strip_placeholders("共 8 行数据")))


def n15_track_bound_key_cross_track_denied() -> None:
    """per-key 白名单轨道绑定（审计 #8）：key 只授权 commerce，却在 research
    上调用白名单内动作 → 拒绝 + 审计（动作对但轨道错，同样是越权）。"""
    from core.auth.tool_registry import PermissionDenied, ToolRegistry, register_key

    register_key("key-com-002", {"read_via_semantic"}, tracks={"commerce"})
    audit: list = []
    reg = ToolRegistry(audit_sink=audit)
    try:
        reg.authorize("L2", "read_via_semantic", "research", key_id="key-com-002")
    finally:
        assert any("DENIED" in c.audit_note and c.key_id == "key-com-002" for c in audit), \
            "跨轨调用必须落审计"


def n16_track_bound_key_in_track_allowed() -> bool:
    """per-key 白名单轨道绑定正例：同一把 key 在本轨调用白名单内动作 → 放行。"""
    from core.auth.tool_registry import ToolRegistry, register_key

    register_key("key-com-003", {"read_via_semantic"}, tracks={"commerce"})
    reg = ToolRegistry()
    call = reg.authorize("L2", "read_via_semantic", "commerce", key_id="key-com-003")
    return bool(call.allowed) and call.audit_note == "allowed"


# P2-01：用例只声明、不执行——执行交给 pytest 参数化或 __main__ 直跑。
# 旧写法在模块导入期就把用例跑完再 sys.exit()，pytest 一收集就 INTERNALERROR。

# 必须「被拒绝」型用例
REJECT_CASES: tuple = (
    ("N1 跨轨路径逃逸", PermissionError, n1_cross_track_escape),
    ("N2 契约缺 grain", ContractError, n2_missing_grain),
    ("N3 契约缺 time_column", ContractError, n3_missing_time_column),
    ("N4 非法 track 名", ValueError, n4_invalid_track),
    ("N5 Semantic API 未知指标", SemanticQueryError, n5_unknown_metric_query),
    ("N7 claim 缺 source 三元组", ClaimError, n7_claim_missing_source_rejected),
    ("N8 台账缺 provenance", LedgerError, n8_ledger_missing_provenance_rejected),
    ("N9 L2 越权动作被拒", PermissionDenied, n9_l2_out_of_whitelist_denied),
    ("N11 per-key 白名单外动作被拒", PermissionDenied, n11_per_key_whitelist_denied),
    ("N13 对账未过的报告拒绝渲染", ReportBlocked, n13_report_blocked_when_claims_mismatch),
    ("N15 跨轨调用被拒（审计 #8）", PermissionDenied, n15_track_bound_key_cross_track_denied),
)

# 必须「通过」型用例（篡改必须被识破 / 比例差异必须不含 welch_t / 口径必须为订单数）
CASES: tuple = (
    ("N6 claim 篡改必须被识破并拦截", n6_tampered_claim_fails),
    ("N10 比例差异不再产出 welch_t", n10_proportion_diff_no_welch),
    ("N12 指标口径回归（订单数 7/8=0.875）", n12_metric_order_count_regression),
    ("N14 模板硬编码数字必被门禁抓到", n14_template_hardcoded_number_detected),
    ("N16 本轨 key 白名单内放行", n16_track_bound_key_in_track_allowed),
)


if __name__ == "__main__":
    sys.exit(casekit.run_cli("TraceableMetrics V1 负面测试（附录 T 规格）",
                             CASES, REJECT_CASES))


test_pass, test_reject, _ = casekit.pytest_cases(CASES, REJECT_CASES)
