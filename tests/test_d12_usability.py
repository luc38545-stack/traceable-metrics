#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D12 真人验收暴露的报告交付反馈回归测试。"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.modeling import dbt as dbt_modeling
from core.quality.health import run_health_check
from core.report.kernel import build_conclusion, render_html
from core.semantic.compiler import load_contract
from plugins.commerce import webapp
import ci_check


def test_dbt_subprocess_forces_utf8_and_preserves_failure_detail(
        monkeypatch, tmp_path: Path) -> None:
    """Windows 中文系统上 dbt 不得用 GBK 解码 UTF-8 工程文件。"""
    seen: dict = {}

    monkeypatch.setattr("shutil.which", lambda _: r"X:\traceable-fake\Scripts\dbt.exe")

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)
        return SimpleNamespace(
            returncode=2,
            stdout="UnicodeDecodeError: 'gbk' codec can't decode byte 0x80",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(dbt_modeling.ModelingError, match="UnicodeDecodeError"):
        dbt_modeling.run_dbt_model(
            tmp_path / "commerce.db", tmp_path / "订单.csv", select="dwd_base"
        )

    assert seen["env"]["PYTHONUTF8"] == "1"
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"
    assert seen["encoding"].lower().replace("-", "") == "utf8"
    assert seen["errors"] == "replace"
    assert seen["args"][seen["args"].index("--log-level") + 1] != "none"


def test_dbt_uses_windows_safe_launcher(monkeypatch) -> None:
    """dbt 调用必须经过本地 launcher，避免 Windows named-pipe 拒绝。"""
    seen: dict = {}
    monkeypatch.setattr("shutil.which", lambda _: r"X:\traceable-fake\Scripts\dbt.exe")

    def fake_run(args, **kwargs):
        seen["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    dbt_modeling.run_dbt_model(Path("x.db"), Path("x.csv"))
    assert seen["args"][1:4] == ["-m", "core.modeling.dbt_runner", "run"]


def test_pipeline_error_payload_exposes_failed_stage() -> None:
    """管道失败必须告诉页面失败阶段，不能只给一条瞬时英文异常。"""
    exc = RuntimeError("dbt run failed")
    exc.pipeline_stage = "建模"
    payload = webapp._pipeline_error_payload(exc)
    assert payload["code"] == "pipeline_failed"
    assert payload["stage"] == "建模"
    assert "dbt run failed" in payload["error"]


def test_action_explain_bounds_optional_llm_wait(monkeypatch, tmp_path: Path) -> None:
    """可选智能说明不得让最终报告预览等待 Provider 的 90 秒默认超时。"""
    root = tmp_path
    run_dir = root / "data" / "runs"
    run_dir.mkdir(parents=True)
    ledger = _ledger_for_readable_report()
    (run_dir / "r-1.json").write_text(
        json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
    )
    seen: dict = {}

    class FakeProvider:
        def __init__(self, *, timeout=None):
            seen["timeout"] = timeout

    def fake_explain(conclusion, provider, face):
        return {
            "narrative": "说明", "claims": [],
            "audit": {"provider": "fake"},
        }

    monkeypatch.setattr(webapp, "ROOT", root)
    monkeypatch.setattr(webapp, "action_claims", lambda _: _clean_ratio_claims())
    monkeypatch.setattr(
        "core.copilot.provider.OpenAICompatibleProvider", FakeProvider
    )
    explain_module = importlib.import_module("core.copilot.explain")
    monkeypatch.setattr(explain_module, "explain", fake_explain)

    out = webapp.action_explain("r-1")
    assert out["status"] == "ok"
    assert seen["timeout"] == 10


def test_commerce_page_has_persistent_report_delivery_status() -> None:
    """用户必须能分清页面报告已生成、文件尚未下载、以及运行失败。"""
    page = webapp.PAGE
    assert 'id="run-status"' in page
    assert 'role="status"' in page
    assert "正在生成指标报告" in page
    assert "报告未生成" in page
    assert "失败阶段" in page
    assert 'id="rp-output-status"' in page
    assert "指标报告已生成（当前页面）" in page
    assert "尚未下载报告文件" in page
    assert "下载报告文件" in page
    assert ".catch(function(e)" in page
    assert 'id="rp-business-summary"' in page
    assert 'id="rp-business-definition"' in page
    assert 'id="rp-technical-details"' in page
    assert "支付成功率 = 成功支付订单数 ÷ 发起支付订单数" in page
    assert "智能说明暂不可用，不影响指标计算和数据核对" in page


def test_report_page_has_optional_final_file_preview() -> None:
    """预览是可选选项卡，下载不得依赖用户先打开预览。"""
    page = webapp.PAGE
    assert 'role="tablist"' in page
    assert 'id="tab-summary"' in page
    assert 'id="tab-preview"' in page
    assert 'id="report-preview"' in page
    assert 'id="rp-preview-frame"' in page
    assert "function ensureReportArtifact" in page
    assert "ensureReportArtifact().then(function(o)" in page

    preview = page.split('id="report-preview"', 1)[1].split(
        '<div class="report-actions"', 1
    )[0]
    assert preview.index('id="rp-preview-frame"') < preview.index('id="rp-share"')
    assert "分享报告" in preview

    export_handler = page.split("$('#btn-export').addEventListener", 1)[1]
    assert "ensureReportArtifact()" in export_handler
    assert "preview" not in export_handler.split("});", 1)[0].lower()


def test_commerce_page_keeps_javascript_newline_escape() -> None:
    """Python 页面模板不得把 JS 字符串里的换行转义提前展开。"""
    assert r"lines.join('\n')" in webapp.PAGE


def test_ci_entry_configures_utf8_console(monkeypatch) -> None:
    """重启后的 GBK 控制台也必须能输出 CI 的中文与检查符号。"""
    calls: list[tuple[str, dict]] = []

    class FakeStream:
        def __init__(self, name: str):
            self.name = name

        def reconfigure(self, **kwargs):
            calls.append((self.name, kwargs))

    monkeypatch.setattr(ci_check.sys, "stdout", FakeStream("stdout"))
    monkeypatch.setattr(ci_check.sys, "stderr", FakeStream("stderr"))
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    ci_check._configure_utf8_console()
    assert calls == [
        ("stdout", {"encoding": "utf-8", "errors": "replace"}),
        ("stderr", {"encoding": "utf-8", "errors": "replace"}),
    ]
    assert ci_check.os.environ["PYTHONUTF8"] == "1"
    assert ci_check.os.environ["PYTHONIOENCODING"] == "utf-8"


def test_ci_pytest_uses_isolated_basetemp_and_no_cacheprovider() -> None:
    """CI 必须避开本机无权限 pytest 临时目录与仓库 cache 钩子。"""
    args = ci_check._pytest_args(skip_browser=False)
    assert args[:3] == ["-m", "pytest", "-q"]
    assert args[args.index("-p") + 1] == "no:cacheprovider"
    basetemp = Path(args[args.index("--basetemp") + 1])
    assert basetemp.name.startswith("traceable_bt_")
    assert not basetemp.exists()
    assert "--ignore=pytest_tmp_current" in args
    assert "--ignore=data" in args
    assert "--ignore=_ci_bt" in args
    assert "--ignore-glob=_ci_bt*" in args
    assert "--ignore-glob=_test_tmp*" in args
    assert "--ignore-glob=.pytest*_tmp" in args


def test_ci_skip_browser_excludes_all_browser_entrypoints() -> None:
    """--skip-browser 必须同时排除独立 D12 与研究轨内嵌的 W19。"""
    args = ci_check._pytest_args(skip_browser=True)
    assert "--ignore=tests/test_d12_browser.py" in args
    assert (
        "--deselect=tests/test_research_webapp.py::test_w19_prereg_plan_enables_run_button"
        in args
    )


def _clean_ratio_claims() -> dict:
    return {
        "claims": [{
            "metric": "pay_success_rate", "period": "2026-08-26",
            "operation": "point", "value": 0.875, "unit": "ratio",
            "source": {"run_id": "r-1", "snapshot_id": "s-1", "query_id": "q-rate"},
            "reconcile": "PASS",
        }],
        "reconcile_diff": "clean", "quality_gate": "passed", "failed": [],
    }


def _ledger_for_readable_report() -> dict:
    return {
        "run_id": "r-1", "track": "commerce", "metric": "pay_success_rate",
        "metric_version": 2, "rows": 8, "snapshot_id": "s-1",
        "metric_series": [["2026-08-26", 0.875]],
        "health_summary": {},
        "provenance": {
            "run_id": "r-1", "snapshot_id": "s-1", "batch_id": "b-1",
            "commit_sha": "a" * 64, "raw_sha256": "b" * 64,
        },
    }


def test_report_leads_with_plain_business_explanation() -> None:
    """业务读者先看到指标含义与分子分母，技术证据只能在折叠附录中。"""
    business_context = {
        "display_name": "支付成功率",
        "definition": "支付成功率 = 成功支付订单数 ÷ 发起支付订单数（订单去重）",
        "unit": "笔",
        "components": {
            "numerator": {
                "metric": "pay_success_orders", "label": "成功支付订单数", "value": 7.0,
                "sentence_label": "支付成功",
                "reconcile": "PASS", "source": {
                    "run_id": "r-1", "snapshot_id": "s-1", "query_id": "q-num",
                },
            },
            "denominator": {
                "metric": "pay_attempted_orders", "label": "发起支付订单数", "value": 8.0,
                "sentence_label": "发起支付订单",
                "reconcile": "PASS", "source": {
                    "run_id": "r-1", "snapshot_id": "s-1", "query_id": "q-den",
                },
            },
        },
        "quality_impact": "金额字段有空白，但本指标不使用金额字段，因此不改变本次结果。",
    }
    conclusion = build_conclusion(
        _ledger_for_readable_report(), _clean_ratio_claims(),
        business_context=business_context,
    )
    html = render_html(conclusion, llm_explanation={
        "status": "unavailable", "reason": "URLError: connection refused",
    })

    assert "<h1>支付成功率报告</h1>" in html
    assert "8 笔发起支付订单中，7 笔支付成功" in html
    assert "支付成功率 = 成功支付订单数 ÷ 发起支付订单数" in html
    assert "无法判断比平时更好还是更差" in html
    assert "金额字段有空白，但本指标不使用金额字段" in html
    assert '<details class="technical">' in html
    assert "智能文字说明暂不可用，不影响指标计算和数据核对" in html
    assert "分享报告" not in html

    main = html.split('<details class="technical">', 1)[0]
    for jargon in (
        "pay_success_rate", "blank_value", "PASS", "run_id", "snapshot_id",
        "raw_sha256", "statistical_executor", "LLM", "ADR-18", "TRACK",
        "amount_total", "amount_paid",
    ):
        assert jargon not in main, f"技术词不应出现在业务正文：{jargon}"


def test_commerce_health_issue_uses_business_column_name() -> None:
    """商用报告正文不应要求用户理解 CSV 英文字段名。"""
    issue = {
        "code": "blank_value", "column": "amount_total", "affected_rows": 1,
        "severity": "warn", "human_text": "「amount_total」列有 1 行空白",
        "impact": "不处理会导致金额类汇总偏低",
        "fix_suggestion": "标记为缺失并按业务规则处理",
    }
    human = webapp._humanize_health_issue(issue)
    assert human["human_text"] == "订单应付金额有 1 行空白"
    assert "amount_total" not in human["human_text"]


def test_pay_success_rate_contract_uses_plain_count_formula() -> None:
    """指标口径应明确是订单数相除，避免把斜杠误读为金额或其它关系。"""
    contract = load_contract(
        Path(__file__).resolve().parent.parent
        / "schemas" / "metrics" / "commerce" / "pay_success_rate.yml"
    )
    assert contract["description"] == (
        "支付成功率 = 成功支付订单数 ÷ 发起支付订单数（订单去重）"
    )


def test_health_summary_keeps_human_issue_details() -> None:
    """导出报告必须拿到列名、影响和行数，不能只拿英文问题代码。"""
    sample = Path(__file__).resolve().parent.parent / "examples" / "测试数据-8月26日订单.csv"
    summary = run_health_check(sample).summary
    details = summary["issue_details"]
    assert details[0]["column"] == "amount_total"
    assert "空白" in details[0]["human_text"]
    assert "汇总偏低" in details[0]["impact"]
    assert details[0]["affected_rows"] == 1


def test_ratio_components_are_queried_through_semantic_api(monkeypatch, tmp_path) -> None:
    """7 和 8 必须各自经 Semantic API 查询登记，不能由比例或行数猜出来。"""
    contract_dir = Path(__file__).resolve().parent.parent / "schemas" / "metrics" / "commerce"
    contract = load_contract(contract_dir / "pay_success_rate.yml")
    calls: list[str] = []

    def fake_query(conn, ledger_db, contract_path, track, run_id=None, limit=None):
        metric = contract_path.stem
        calls.append(metric)
        value = 7.0 if metric == "pay_success_orders" else 8.0
        return {
            "query_id": f"q-{metric}", "metric": metric, "metric_version": 1,
            "freshness": "PT30M", "rows": [["2026-08-26", value]],
        }

    monkeypatch.setattr("core.semantic.api.query_metric", fake_query)
    out = webapp._query_ratio_components(
        object(), tmp_path / "ledger.db", contract_dir, contract,
        track="commerce", run_id="r-1",
    )
    assert calls == ["pay_success_orders", "pay_attempted_orders"]
    assert out["numerator"]["query_id"] == "q-pay_success_orders"
    assert out["denominator"]["series"] == [["2026-08-26", 8.0]]
