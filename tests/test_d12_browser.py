#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D12 主旅程自动化预演（真实浏览器）—— 真人测试前的最后一道闸。

为什么要有这个脚本：真人测试(D12)只能做一次，不能卡在低级缺陷上。
本脚本用真实 Chromium 走完 上传→体检→分析→分享 四步，
重点覆盖**拖拽上传**这条路径——它不走 file input，是人工最容易踩的分支。

依赖：本机已有 playwright + Chromium（无则 `pip install playwright && playwright install chromium`）
用法（需先启动 webapp：见 readme 运行第 4 步）：
    python tests/test_d12_browser.py
    pytest -q tests/test_d12_browser.py     # 缺 playwright 或服务没起则自动 skip
产物：tests/d12/screenshots/ 下的分步截图
"""
from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "测试数据-8月26日订单.csv"
SHOTS = ROOT / "tests" / "d12" / "screenshots"
BASE = "http://127.0.0.1:8765"

# 本机回环一律直连：环境里设了 http_proxy 时，urllib 会把 127.0.0.1 也发给代理。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)


def server_up(base: str = BASE) -> bool:
    try:
        urllib.request.urlopen(base, timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False

# 模拟真实拖拽：构造 DataTransfer 并派发 drop 事件（不经过 file input）
DROP_JS = """
([content, name]) => {
  const dz = document.querySelector('#drop');
  const dt = new DataTransfer();
  dt.items.add(new File([content], name, {type: 'text/csv'}));
  dz.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true}));
  dz.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true}));
  return true;
}
"""


def run_checks() -> list[tuple[str, bool, str]]:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    checks: list[tuple[str, bool, str]] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 1100})
        # 捕获前端未捕获异常 —— 拖拽分支曾在此静默崩溃
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)

        page.goto(BASE, wait_until="load")
        page.wait_for_selector("#drop")
        page.screenshot(path=str(SHOTS / "01-投料页.png"))
        checks.append(("0 页面加载", "数据投料" in page.inner_text("h1"), ""))

        # 步骤 1-2：拖拽投料 → 自动体检
        page.evaluate(DROP_JS, [SAMPLE.read_text(encoding="utf-8"), SAMPLE.name])
        page.wait_for_selector("#checkup-card:not(.hidden)", timeout=15000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOTS / "02-体检报告.png"))

        ck_text = page.inner_text("#checkup-card")
        checks.append(("1 拖拽后体检卡片出现", "体检报告" in ck_text, ""))
        checks.append(("1b 文件名正确显示", SAMPLE.name in page.inner_text("#ck-file"),
                       page.inner_text("#ck-file")))
        checks.append(("1c 行数识别", page.inner_text("#ck-rows").strip() == "8",
                       page.inner_text("#ck-rows")))
        checks.append(("1d 无 JS 异常", not errors, "; ".join(errors[:2])))

        # 步骤 3：生成报告
        page.click("#btn-run")
        page.wait_for_selector("#pg-report:not(.hidden)", timeout=60000)
        page.wait_for_selector("#rp-claims-body", timeout=15000)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(SHOTS / "03-指标报告.png"), full_page=True)

        value = page.inner_text("#rp-value")
        gate = page.inner_text("#rp-gate")
        checks.append(("2 报告页出现", "支付成功率报告" in page.inner_text("#pg-report h1"), ""))
        checks.append(("2b 指标值", value.strip() == "87.50%", value))
        checks.append(("2c 对账闸门", "已核对" in gate, gate))
        checks.append(("2d 对账说明可读", "重新计算" in page.inner_text("#rp-claims-body"), ""))
        summary = page.inner_text("#rp-business-summary")
        checks.append(("2e 业务解释含分子分母", "8 笔发起支付订单" in summary and "7 笔支付成功" in summary, summary))

        # 步骤 4：可选预览。它生成的就是最终 HTML，但不触发下载。
        page.click("#tab-preview")
        page.wait_for_selector("#report-preview:not(.hidden)", timeout=15000)
        page.wait_for_selector("#rp-share:not(.hidden)", timeout=30000)
        preview_body = page.frame_locator("#rp-preview-frame").locator("body").inner_text(
            timeout=15000
        )
        preview_status = page.inner_text("#rp-preview-status")
        checks.append(("3 最终文件可预览", "支付成功率报告" in preview_body,
                       preview_status))
        checks.append(("3a 预览不触发下载", "预览不是下载前置步骤" in preview_status,
                       preview_status))
        checks.append(("3b 分享说明不混入报告", "分享报告" not in preview_body, ""))
        below = page.locator("#rp-share").evaluate(
            "el => el.getBoundingClientRect().top > "
            "document.querySelector('#rp-preview-frame').getBoundingClientRect().bottom"
        )
        checks.append(("3c 分享说明位于预览下方", bool(below), ""))

        # 下载独立按钮始终可用；已预览时复用同一份文件。
        with page.expect_download(timeout=30000) as dl_info:
            page.click("#btn-export")
        download = dl_info.value
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "04-预览及分享.png"), full_page=True)

        share = page.inner_text("#rp-share-body")
        checks.append(("3d 报告可下载", bool(download.suggested_filename),
                       download.suggested_filename))
        checks.append(("3e ADR-14 轨道前缀",
                       download.suggested_filename.startswith("[C]"),
                       download.suggested_filename[:2]))
        checks.append(("3f 分享区给出服务器副本", "服务器副本" in share, ""))
        checks.append(("3g 分享区给出文件指纹", "文件指纹" in share, ""))
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        mobile_widths = page.evaluate(
            "() => ({scroll: document.body.scrollWidth, client: document.body.clientWidth})"
        )
        checks.append(("3h 手机宽度无横向溢出",
                       mobile_widths["scroll"] <= mobile_widths["client"],
                       str(mobile_widths)))
        page.set_viewport_size({"width": 1000, "height": 1100})

        # 打开导出的报告本体，确认它是一份能独立发给人的文件
        report_path = SHOTS / download.suggested_filename
        download.save_as(str(report_path))
        page.goto(report_path.as_uri(), wait_until="load")
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / "05-导出报告成品.png"), full_page=True)
        body = page.inner_text("body")
        checks.append(("4 导出报告标题可读", "支付成功率报告" in body and "商用轨" in body, ""))
        jargon = ("pay_success_rate", "blank_value", "PASS", "run_id",
                  "snapshot_id", "raw_sha256", "LLM", "TRACK")
        visible_jargon = [word for word in jargon if word in body]
        checks.append(("4a 正文默认不露技术词", not visible_jargon,
                       ", ".join(visible_jargon)))
        checks.append(("4b 导出报告先讲中文口径",
                       "支付成功率 = 成功支付订单数 ÷ 发起支付订单数" in body, ""))
        checks.append(("4c 导出报告解释单日边界",
                       "无法判断比平时更好还是更差" in body, ""))
        checks.append(("4d 数据问题使用中文字段名",
                       "订单应付金额" in body and "订单实付金额" in body, ""))

        # 追溯证据仍完整保留，但只在用户主动展开技术附录后出现。
        page.click("details.technical summary")
        technical_body = page.inner_text("body")
        checks.append(("4e 技术附录含轨标识", "TRACK · 商用轨" in technical_body, ""))
        checks.append(("4f 技术附录含溯源",
                       "raw_sha256" in technical_body and "snapshot_id" in technical_body, ""))
        checks.append(("4g 技术附录含未检项", "未检项清单" in technical_body, ""))
        m = re.search(r"run_id\s+(\S+)", technical_body)
        checks.append(("4h 技术附录含 run_id", bool(m), m.group(1) if m else ""))

        browser.close()

    return checks


def main() -> int:
    checks = run_checks()
    failed = [c for c in checks if not c[1]]
    print("=" * 60)
    print("D12 主旅程自动化预演（真实浏览器 · 拖拽路径）")
    print("=" * 60)
    for label, ok, detail in checks:
        print(f"  {'✓' if ok else '✗'} {label:<24} {detail}")
    print("-" * 60)
    print(f"{len(checks) - len(failed)}/{len(checks)} passed · 截图见 tests/d12/screenshots/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())


# ---- P2-01/P2-02：同一批断言同时暴露给标准 pytest ----
import pytest  # noqa: E402


@pytest.mark.skipif(not server_up(), reason="webapp 未启动（readme 运行第 4 步）")
def test_d12_main_journey() -> None:
    pytest.importorskip("playwright", reason="未安装 playwright")
    checks = run_checks()
    failed = [(n, d) for n, ok, d in checks if not ok]
    assert not failed, "D12 主旅程未通过：\n" + "\n".join(f"  - {n}: {d}" for n, d in failed)
