#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraceableMetrics 单一 CI 入口（批次 D · P2-01 第 4/5 条）。

把分散的门禁/测试收敛成一个命令，按固定顺序执行：
  1. 架构门禁（tests/check_dependencies.py，R1-R9 纯静态，不依赖第三方库）
  2. 运行环境门禁（tests/check_runtime.py，RT1-RT6 本机真跑条件）
  3. 标准 pytest 全量（单元/负面/统计/AB/可信度/安全/E2E；D12 浏览器测试在
     webapp 未启动时自动 skip，不影响退出码）

用法（任选其一）：
  python ci_check.py                  # 直接运行
  python ci_check.py --skip-browser   # 跳过浏览器 E2E（未装 playwright 时用）
  （Windows 双击 ci_check.bat 等同第一条）

退出码：0 = 全绿；非 0 = 有门禁或测试失败（首处失败即停，便于定位）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
GATES: list[tuple[str, list[str]]] = [
    ("架构门禁 architecture gate (R1-R9)", ["tests/check_dependencies.py"]),
    ("运行环境门禁 runtime gate (RT1-RT6)", ["tests/check_runtime.py"]),
]


def _configure_utf8_console() -> None:
    """固定本进程及门禁子进程编码，避免中文 Windows 的 GBK 输出中断 CI。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"


def _pytest_args(skip_browser: bool) -> list[str]:
    """生成本机安全的 pytest 参数，避开受保护的默认 temp/cache 目录。"""
    basetemp = Path(tempfile.gettempdir()) / (
        f"traceable_bt_{os.getpid()}_{time.time_ns()}"
    )
    args = [
        "-m", "pytest", "-q",
        "-p", "no:cacheprovider",
        "--basetemp", str(basetemp),
        "--ignore=pytest_tmp_current",
        "--ignore=data",
        "--ignore=_ci_bt",
        "--ignore-glob=_ci_bt*",
        "--ignore-glob=_test_tmp*",
        "--ignore-glob=.pytest*_tmp",
        "--ignore-glob=pytest_tmp*",
        "--ignore-glob=_review*",
        "--ignore-glob=*_bt_*",
        "--ignore-glob=traceable_probe_*",
    ]
    if skip_browser:
        # 浏览器用例既有独立 D12 文件，也有研究网页契约中的 W19。
        args += [
            "--ignore=tests/test_d12_browser.py",
            "--deselect=tests/test_research_webapp.py::test_w19_prereg_plan_enables_run_button",
        ]
    return args


def _run(label: str, cmd: list[str]) -> bool:
    print(f"\n=== {label} ===")
    r = subprocess.run([PY, *cmd], cwd=str(ROOT))
    if r.returncode != 0:
        print(f"  ✗ {label} 失败（退出码 {r.returncode}）")
        return False
    print(f"  ✓ {label} 通过")
    return True


def main() -> int:
    _configure_utf8_console()
    skip_browser = "--skip-browser" in sys.argv
    for label, args in GATES:
        if not _run(label, args):
            return 1
    print("\n=== 标准 pytest 全量 ===")
    pytest_args = _pytest_args(skip_browser)
    r = subprocess.run([PY, *pytest_args], cwd=str(ROOT))
    if r.returncode != 0:
        print(f"  ✗ pytest 失败（退出码 {r.returncode}）")
        return 1
    print("  ✓ pytest 全量通过")
    print("\nCI CHECK: ALL PASS ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
