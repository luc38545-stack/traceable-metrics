#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 修复回归：D7 确定性脱敏 CLI（core.governance.redact_cli）。

覆盖：
- 正常路径：exit 0、脱敏副本落盘、原始输入逐字节不变（D7 输入不可变）
- 同路径拒绝：--out == --rows → exit 1，原始文件完好（P2 反例）
- 非法 --level → exit 1；rows 文件不存在 → exit 1
- rows 内容非法（非对象数组）→ exit 1
- 子进程端到端：stdout 单行 JSON 可解析，status=OK
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROWS = [
    {"order_id": "SO-1001", "customer_phone": "13800001111", "amount_total": 42.0},
    {"order_id": "SO-1002", "customer_phone": "13800002222", "amount_total": 99.5},
]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "core.governance.redact_cli", *args],
        capture_output=True, text=True,
    )


def _write_rows(tmp_path: Path, rows=ROWS) -> tuple[Path, bytes]:
    rf = tmp_path / "rows.json"
    raw = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    rf.write_bytes(raw)
    return rf, raw


def test_ok_path_writes_redacted_copy_and_keeps_input_immutable(tmp_path) -> None:
    rf, raw = _write_rows(tmp_path)
    proc = _run_cli("--rows", str(rf), "--level", "internal")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip().splitlines()[0])
    assert payload["status"] == "OK"
    out = Path(payload["out"])
    assert out.exists() and out != rf
    # 原始输入逐字节不变
    assert rf.read_bytes() == raw
    redacted = json.loads(out.read_text(encoding="utf-8"))
    # internal 级：机密字段（电话）被掩码
    assert redacted[0]["customer_phone"] != "13800001111"


def test_reject_same_out_path(tmp_path) -> None:
    """[P2 反例] --out 与 --rows 同一路径 → exit 1，原始文件完好。"""
    rf, raw = _write_rows(tmp_path)
    proc = _run_cli("--rows", str(rf), "--level", "public", "--out", str(rf))
    assert proc.returncode == 1
    payload = json.loads(proc.stdout.strip().splitlines()[0])
    assert payload["status"] == "ERROR"
    assert "禁止覆盖原始输入" in payload["reason"]
    assert rf.read_bytes() == raw  # 未被覆盖


def test_reject_invalid_level(tmp_path) -> None:
    rf, _ = _write_rows(tmp_path)
    proc = _run_cli("--rows", str(rf), "--level", "topsecret")
    assert proc.returncode == 1
    payload = json.loads(proc.stdout.strip().splitlines()[0])
    assert payload["status"] == "ERROR"


def test_reject_missing_rows_file(tmp_path) -> None:
    proc = _run_cli("--rows", str(tmp_path / "nope.json"), "--level", "public")
    assert proc.returncode == 1


def test_reject_invalid_rows_payload(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    proc = _run_cli("--rows", str(bad), "--level", "public")
    assert proc.returncode == 1
    payload = json.loads(proc.stdout.strip().splitlines()[0])
    assert payload["status"] == "ERROR"


def test_reject_inconsistent_row_fields(tmp_path) -> None:
    """字段集合不一致的批量行 → 显性拒绝（redact_rows 语义穿透到 CLI）。"""
    rf, _ = _write_rows(tmp_path, rows=[
        {"a": 1, "b": 2},
        {"a": 1, "c": 3},
    ])
    proc = _run_cli("--rows", str(rf), "--level", "internal")
    assert proc.returncode == 1
