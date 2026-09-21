#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V1 端到端冒烟（含报告导出）—— D12 真人测试前的自动化自检。

覆盖架构书 V1-DoD 技术侧主链：
  上传 → 体检 → raw 落地 → dbt 建模 → 语义编译 → 指标查询 → 快照+台账
  → claims 对账 → HTML 报告导出（ADR-18 闸门 + ADR-14 徽章）

用法（需先启动 webapp：见 readme 运行第 4 步）：
  python tests/test_e2e_export.py [http://127.0.0.1:8765]
  pytest -q tests/test_e2e_export.py     # 服务未启动则自动 skip
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "测试数据-8月26日订单.csv"
DEFAULT_BASE = "http://127.0.0.1:8765"

# 本机回环一律直连：环境里若设了 http_proxy（沙箱/公司代理），
# urllib 会把 127.0.0.1 也发给代理，拿到 502 Bad Gateway。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)


def _post_upload(base: str, path: Path, token: str) -> dict:
    boundary = uuid.uuid4().hex
    head = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n\r\n'
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + path.read_bytes() + tail
    req = urllib.request.Request(
        f"{base}/api/upload", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-CSRF-Token": token},   # P1-05：POST 必须带启动令牌
    )
    return json.loads(urllib.request.urlopen(req).read())


def _get(base: str, path: str) -> dict:
    return json.loads(urllib.request.urlopen(f"{base}{path}").read())


def _post(base: str, path: str, token: str, headers: dict | None = None) -> dict:
    h = {"X-CSRF-Token": token}
    h.update(headers or {})
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(f"{base}{path}", method="POST", headers=h)).read())


def _session_token(base: str) -> str:
    """P1-05 ①：本机会话认证——从 /api/session 取启动令牌。"""
    return _get(base, "/api/session")["csrf_token"]


def server_up(base: str = DEFAULT_BASE) -> bool:
    """P2-01：pytest 收集到本机服务类测试时先探活，服务没起就 skip 而不是 fail。"""
    try:
        urllib.request.urlopen(base, timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def run_checks(base: str = DEFAULT_BASE) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    token = _session_token(base)
    checks.append(("0 会话令牌", bool(token) and len(token) == 32, token[:6] + "…"))

    up = _post_upload(base, SAMPLE, token)
    checks.append(("1 上传", bool(up.get("upload_id")), str(up.get("name"))))
    uid = up["upload_id"]

    ck = _get(base, f"/api/checkup?id={urllib.parse.quote(uid)}")
    checks.append(("2 体检", ck.get("rows") == 8, f"行数 {ck.get('rows')}"))

    # P1-07：带幂等键——重复请求必须返回同一 run，不重复建批次
    idem = f"e2e-{uuid.uuid4().hex[:8]}"
    run = _post(base, f"/api/run?id={urllib.parse.quote(uid)}", token,
                headers={"Idempotency-Key": idem})
    checks.append(("3 管道跑通", bool(run.get("run_id")), str(run.get("run_id"))))

    again = _post(base, f"/api/run?id={urllib.parse.quote(uid)}", token,
                  headers={"Idempotency-Key": idem})
    checks.append(("3h 幂等键重复请求返回同一 run",
                   again.get("run_id") == run.get("run_id"),
                   f"{run.get('run_id')} vs {again.get('run_id')}"))

    checks.append(("3b 指标口径回归", run.get("metric_series") == [["2026-08-26", 0.875]],
                   str(run.get("metric_series"))))

    cl = _get(base, f"/api/claims?run={run['run_id']}")
    checks.append(("4 claims 对账", cl.get("reconcile_diff") == "clean",
                   cl.get("quality_gate", "")))

    resp = urllib.request.urlopen(f"{base}/api/export?run={run['run_id']}")
    html = resp.read().decode("utf-8")
    name = urllib.parse.unquote(resp.headers.get("X-Export-Name", ""))
    sha = resp.headers.get("X-Export-Sha256", "")
    checks.append(("5 报告导出", bool(name) and len(sha) == 64, name))
    checks.append(("5b ADR-14 轨道前缀", name.startswith("[C]"), name[:2]))
    checks.append(("5c 页眉轨徽章", "TRACK · 商用轨" in html, ""))
    checks.append(("5d 对账段存在", "数字对账（ADR-18）" in html, ""))
    checks.append(("5e 溯源页脚", "raw_sha256" in html and "snapshot_id" in html, ""))
    checks.append(("5f 未检项清单", "未检项清单" in html, ""))
    checks.append(("5g 核心指标渲染", 'class="big">87.50%<' in html, ""))

    return checks


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    checks = run_checks(base)
    failed = [c for c in checks if not c[1]]
    for label, ok, detail in checks:
        print(f"  {'✓' if ok else '✗'} {label:<20} {detail}")
    print(f"\nE2E: {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())


# ---- P2-01：同一批断言同时暴露给标准 pytest ----
import pytest  # noqa: E402


@pytest.mark.skipif(not server_up(), reason="webapp 未启动（readme 运行第 4 步）")
def test_e2e_main_chain() -> None:
    checks = run_checks()
    failed = [(n, d) for n, ok, d in checks if not ok]
    assert not failed, "E2E 未通过：\n" + "\n".join(f"  - {n}: {d}" for n, d in failed)
