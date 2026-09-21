#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段2 回归 + P1 修复反例：D6 独立闸门 CLI（core.copilot.gate）。

覆盖：
- [正常] claims 数字与被引用 claim 一致 → PASS（退出码 0）
- [P1-1 反例] citations=[] 但数字在银行里 → 必须 FAIL（不得绕过引用）
- [P1-1 反例] 数字在未引用 claim 上 → FAIL（strict 语义）
- [正常] 正文出现银行外数字 → FAIL（退出码 2）
- [正常] citations 指向不存在的 key → FAIL（退出码 2）
- [P1-2 反例] claim.source.run_id 与台账不一致（跨 run 混用）→ BLOCKED
- [P1-2 反例] claim.source.query_id 与台账不一致 → BLOCKED
- [P1-2 反例] provenance.snapshot_id 与台账本体不一致 → BLOCKED
- [P1-2 反例] 快照文件 SHA-256 与台账声明不一致 → BLOCKED
- [P1-2 反例] 未提供快照文件 → BLOCKED（不接受字段级检查）
- [正常] 缺 provenance 字段 → BLOCKED（退出码 3）
- [正常] claims 对账不通过 → BLOCKED（退出码 3）
- [正常] schema_version 缺失/不受支持 → 显式终止（退出码 4）
- [正常] CLI 子进程端到端：退出码 + stdout JSON
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.copilot.gate import run_gate

CLAIM = {
    "metric": "pay_success_rate",
    "period": "2026-08-26",
    "value": 0.8125,
    "unit": "rate",
    "reconcile": "PASS",
    "source": {
        "query_id": "q-test-0001",
        "run_id": "r-test-0001",
        "snapshot_id": "s-test-0001",
    },
}
CLAIMS_CLEAN = {"reconcile_diff": "clean", "claims": [CLAIM], "failed": []}
SNAPSHOT_BYTES = b"fake-duckdb-snapshot-bytes-for-gate-tests"


def _ledger(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "run_id": "r-test-0001",
        "track": "commerce",
        "started_at": "2026-08-26T10:00:00",
        "batch_id": "b-test-0001",
        "snapshot_id": "s-test-0001",
        "snapshot_sha256": hashlib.sha256(SNAPSHOT_BYTES).hexdigest(),
        "snapshot_size": len(SNAPSHOT_BYTES),
        "rows": 8,
        "metric": "pay_success_rate",
        "metric_version": 1,
        "metric_series": [["2026-08-26", 0.8125]],
        "query_id": "q-test-0001",
        "health_summary": {},
        "steps": [],
        "provenance": {
            "run_id": "r-test-0001",
            "snapshot_id": "s-test-0001",
            "batch_id": "b-test-0001",
            "commit_sha": "c" * 40,
        },
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, ledger: dict, claims: dict, narrative: str,
           citations: list[str] | None = None,
           snapshot_bytes: bytes | None = SNAPSHOT_BYTES,
           with_snapshot: bool = True) -> tuple[Path, Path, Path, Path | None]:
    lf = tmp_path / "run.json"; lf.write_text(json.dumps(ledger, ensure_ascii=False), "utf-8")
    cf = tmp_path / "claims.json"; cf.write_text(json.dumps(claims, ensure_ascii=False), "utf-8")
    nf = tmp_path / "narrative.json"
    nf.write_text(json.dumps({"narrative": narrative, "citations": citations or []},
                             ensure_ascii=False), "utf-8")
    sf = None
    if with_snapshot:
        sf = tmp_path / "snapshot.db"
        sf.write_bytes(snapshot_bytes if snapshot_bytes is not None else b"")
    return nf, lf, cf, sf


def test_pass_when_numbers_match_claims(tmp_path) -> None:
    """正文数字与被引用 claim 精确一致 → PASS（退出码 0）。"""
    nf, lf, cf, sf = _write(tmp_path, _ledger(), CLAIMS_CLEAN,
                            "当日支付成功率 {claim:pay_success_rate#2026-08-26}，即 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 0
    assert result["status"] == "PASS"


def test_fail_when_citations_empty_even_if_number_in_bank(tmp_path) -> None:
    """[P1-1 反例] citations=[] 且数字恰在银行 → 必须 FAIL，不得绕过引用。"""
    nf, lf, cf, sf = _write(tmp_path, _ledger(), CLAIMS_CLEAN,
                            "当日支付成功率 81.25%。", citations=[])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 2
    assert result["status"] == "FAIL"


def test_fail_on_uncited_bank_number(tmp_path) -> None:
    """[P1-1 反例] 数字在银行但未被引用（strict 语义）→ FAIL。"""
    bank2 = dict(CLAIMS_CLEAN, claims=[
        CLAIM,
        dict(CLAIM, period="2026-08-27", value=0.9),
    ])
    nf, lf, cf, sf = _write(tmp_path, _ledger(), bank2,
                            "昨日为 90.00%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 2
    assert result["status"] == "FAIL"


def test_fail_on_unauthorized_number(tmp_path) -> None:
    """正文出现 claims 里不存在的数字 → FAIL（退出码 2），失败单含上下文。"""
    nf, lf, cf, sf = _write(tmp_path, _ledger(), CLAIMS_CLEAN,
                            "当日支付成功率 81.25%，昨天是 77.7%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 2
    assert result["status"] == "FAIL"
    assert any(u["number"] == "77.7%" for u in result["unmatched_numbers"])


def test_fail_on_bad_citation(tmp_path) -> None:
    """citations 指向银行里不存在的 key → FAIL（退出码 2）。"""
    nf, lf, cf, sf = _write(tmp_path, _ledger(), CLAIMS_CLEAN,
                            "支付成功率 81.25%。",
                            citations=["pay_success_rate#1999-01-01"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 2
    assert result["bad_citations"] == ["pay_success_rate#1999-01-01"]


def test_blocked_when_claim_source_run_mismatch(tmp_path) -> None:
    """[P1-2 反例] claim.source.run_id 与台账不一致（跨 run 混用）→ BLOCKED。"""
    mixed = dict(CLAIMS_CLEAN, claims=[
        dict(CLAIM, source=dict(CLAIM["source"], run_id="r-other-run")),
    ])
    nf, lf, cf, sf = _write(tmp_path, _ledger(), mixed,
                            "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 3
    assert "run_id" in result["reason"]


def test_blocked_when_claim_source_query_mismatch(tmp_path) -> None:
    """[P1-2 反例] claim.source.query_id 与台账不一致 → BLOCKED。"""
    mixed = dict(CLAIMS_CLEAN, claims=[
        dict(CLAIM, source=dict(CLAIM["source"], query_id="q-other")),
    ])
    nf, lf, cf, sf = _write(tmp_path, _ledger(), mixed,
                            "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 3


def test_blocked_when_provenance_inconsistent_with_ledger(tmp_path) -> None:
    """[P1-2 反例] provenance.snapshot_id ≠ 台账本体 snapshot_id → BLOCKED。"""
    led = _ledger()
    led["provenance"] = dict(led["provenance"], snapshot_id="s-some-other-run")
    nf, lf, cf, sf = _write(tmp_path, led, CLAIMS_CLEAN,
                            "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 3
    assert "不一致" in result["reason"]


def test_blocked_when_snapshot_sha_mismatch(tmp_path) -> None:
    """[P1-2 反例] 快照文件 SHA-256 与台账声明不一致（篡改）→ BLOCKED。"""
    nf, lf, cf, sf = _write(tmp_path, _ledger(), CLAIMS_CLEAN,
                            "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"],
                            snapshot_bytes=b"tampered-content")
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 3
    assert "SHA-256 不一致" in result["reason"]


def test_blocked_when_snapshot_not_provided(tmp_path) -> None:
    """[P1-2 反例] 未提供快照文件 → BLOCKED（字段级检查不被接受）。"""
    nf, lf, cf, _ = _write(tmp_path, _ledger(), CLAIMS_CLEAN,
                           "支付成功率 81.25%。",
                           citations=["pay_success_rate#2026-08-26"],
                           with_snapshot=False)
    code, result = run_gate(nf, lf, cf, snapshot_path=None)
    assert code == 3
    assert "--snapshot" in result["reason"]


def test_blocked_when_provenance_missing(tmp_path) -> None:
    """provenance 缺 snapshot_id → BLOCKED（退出码 3），数字不可溯源。"""
    led = _ledger()
    del led["provenance"]["snapshot_id"]
    nf, lf, cf, sf = _write(tmp_path, led, CLAIMS_CLEAN, "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 3
    assert result["status"] == "BLOCKED"
    assert "snapshot_id" in result["reason"]


def test_blocked_when_reconcile_not_clean(tmp_path) -> None:
    """claims 对账未通过 → BLOCKED（退出码 3，ReportBlocked 路径）。"""
    bad = dict(CLAIMS_CLEAN, reconcile_diff="dirty", failed=[{"period": "2026-08-26"}])
    nf, lf, cf, sf = _write(tmp_path, _ledger(), bad, "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 3
    assert result["status"] == "BLOCKED"


@pytest.mark.parametrize("sv,expect_status", [
    (2, "SCHEMA_UNSUPPORTED"),
    (None, "SCHEMA_MISSING"),
])
def test_schema_version_explicit_reject(tmp_path, sv, expect_status) -> None:
    """schema_version 缺失或不受支持 → 显式终止（退出码 4），绝不猜测。"""
    led = _ledger(schema_version=sv)
    nf, lf, cf, sf = _write(tmp_path, led, CLAIMS_CLEAN, "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    code, result = run_gate(nf, lf, cf, snapshot_path=sf)
    assert code == 4
    assert result["status"] == expect_status


def test_cli_subprocess_end_to_end(tmp_path) -> None:
    """CLI 子进程端到端：退出码 0 + stdout 单行 JSON 可解析。"""
    nf, lf, cf, sf = _write(tmp_path, _ledger(), CLAIMS_CLEAN,
                            "支付成功率 81.25%。",
                            citations=["pay_success_rate#2026-08-26"])
    proc = subprocess.run(
        [sys.executable, "-m", "core.copilot.gate",
         "--narrative", str(nf), "--ledger", str(lf), "--claims", str(cf),
         "--snapshot", str(sf)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip().splitlines()[0])
    assert payload["status"] == "PASS"
