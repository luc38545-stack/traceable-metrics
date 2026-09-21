#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""traceable 运行环境门禁（runtime gate）

与 tests/check_dependencies.py（architecture gate，纯静态扫描）互补：
架构门禁只回答"依赖方向对不对"，不依赖任何第三方库，缺 duckdb 也 PASS；
运行门禁回答"本机环境能不能真跑"，缺任何一个运行时依赖都必须显性 FAIL。

检查项（P2-03 修改意见）：
  RT1 正确 Python 解释器（版本满足最低要求，能 import 关键包）
  RT2 requirements.lock.txt 全部可导入
  RT3 dbt CLI 可执行（dbt --version 能返回）
  RT4 关键版本满足 requirements.lock.txt 锁定（逐包 == 精确比对）
  RT5 启动器与 README 使用同一解释器（bat 主 PY == README 声明路径）
  RT6（提示）未检测到项目 .venv / 锁定环境 —— 建议运行 setup.bat 创建隔离环境

用法：python tests/check_runtime.py     （退出码 0=通过，1=失败）
"""
from __future__ import annotations

import importlib
import importlib.metadata
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBLEMS: list[str] = []
WARNINGS: list[str] = []

MIN_PY = (3, 12)  # 发布基线：锁文件由 3.12 干净环境生成，低版本不承诺可用


def rt1_interpreter() -> None:
    """RT1 正确 Python 解释器：版本满足要求且可导入关键包。"""
    if sys.version_info < MIN_PY:
        PROBLEMS.append(
            f"RT1 python version {sys.version_info.major}.{sys.version_info.minor} < "
            f"required {MIN_PY[0]}.{MIN_PY[1]}"
        )
    for mod in ("duckdb", "yaml", "dbt"):
        try:
            importlib.import_module(mod)
        except ImportError as e:
            PROBLEMS.append(f"RT1 cannot import {mod}: {e}")


def _norm(name: str) -> str:
    """PEP 503 发行名规范化：大小写与 -/_/. 等价。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_entries() -> list[tuple[str, str, str]]:
    """解析 requirements.lock.txt 顶层锁定，返回 [(name, op, version), ...]。

    只取 `name==version` 约束行；`# via` 注释与空行跳过。
    """
    out = []
    req = ROOT / "requirements.lock.txt"
    if not req.exists():
        WARNINGS.append("RT2 requirements.lock.txt 不存在，跳过依赖清单核对")
        return out
    for line in req.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(>=|==|<=|~=)\s*([0-9][0-9A-Za-z.+\-]*)$", line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def rt2_requirements() -> None:
    """RT2 requirements.lock.txt 逐包已安装（元数据比对，不做 import——
    锁文件含传递依赖，其发行名不等于 import 名，如 pydantic-core/typing-extensions）。"""
    for name, _op, _ver in _lock_entries():
        if _installed_version(name) is None:
            PROBLEMS.append(f"RT2 not installed: {name} (from requirements.lock.txt)")


def _ver_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v.split("+")[0].split("-")[0])
    return tuple(int(n) for n in nums) or (0,)


def _installed_version(name: str) -> str | None:
    """已装版本；未装 → None（不抛）。

    importlib.metadata.version('pyyaml') 在部分环境对大小写敏感的 dist
    名（PyYAML）返回 None 而非抛 PackageNotFoundError（实测：have=None 直接
    崩了 _ver_tuple）。这里用大小写无关的 distribution 扫描兜底。
    """
    try:
        v = importlib.metadata.version(name)
        if v:
            return v
    except importlib.metadata.PackageNotFoundError:
        pass
    want = name.lower()
    for dist in importlib.metadata.distributions():
        try:
            dn = (dist.metadata.get("Name") or "").lower()
        except Exception:  # noqa: BLE001 — 单个 dist 元数据损坏不影响其余
            dn = ""
        if dn == want:
            return dist.version
    return None


def rt3_dbt_cli() -> None:
    """RT3 dbt CLI 可执行。"""
    venv_bin = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    candidates = [
        venv_bin / ("dbt.exe" if sys.platform == "win32" else "dbt"),
        Path(shutil.which("dbt")) if shutil.which("dbt") else None,
    ]
    exe = next((str(cand) for cand in candidates if cand and cand.exists()), None)
    if not exe:
        PROBLEMS.append("RT3 dbt CLI 不可执行：项目 .venv 和 PATH 均未找到 dbt")
        return
    try:
        r = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            PROBLEMS.append(f"RT3 dbt --version 退出码 {r.returncode}: {r.stderr.strip()[:200]}")
        elif not re.search(r"dbt|Core:|installed", r.stdout + r.stderr, re.I):
            PROBLEMS.append("RT3 dbt --version 输出异常（未识别 dbt）")
    except (OSError, subprocess.TimeoutExpired) as e:
        PROBLEMS.append(f"RT3 dbt 执行失败: {e}")


def rt4_versions() -> None:
    """RT4 已装版本满足 requirements.lock.txt 锁定（== 精确比对）。"""
    for name, op, need in _lock_entries():
        if op != "==":
            continue
        have = _installed_version(name)
        if have is None:
            continue  # 未安装 → RT2 已报
        if _ver_tuple(have) != _ver_tuple(need):
            PROBLEMS.append(
                f"RT4 {name} 已装 {have} != 锁定 {need}（requirements.lock.txt）"
            )


def rt5_launcher_consistency() -> None:
    """RT5 启动器使用项目虚拟环境或 PATH 中的 Python，不绑定开发者机器。"""
    bat = ROOT / "启动工作台.bat"
    """RT5 启动器使用项目虚拟环境或 PATH 中的 Python，不绑定开发者机器。"""
    if not bat.exists():
        PROBLEMS.append("RT5 启动工作台.bat 不存在")
        return
    text = bat.read_text(encoding="utf-8", errors="ignore").lower()
    if "d:\\anaconda" in text or "c:\\users\\" in text:
        PROBLEMS.append("RT5 启动器包含开发者机器绝对 Python 路径")
    if "venv\\scripts\\python.exe" not in text or "where python" not in text:
        PROBLEMS.append("RT5 启动器未提供项目 .venv 与 PATH Python 的可移植回退")


def rt6_venv_hint() -> None:
    """RT6（提示）项目 .venv / 锁定环境。"""
    venv = ROOT / ".venv"
    uv_lock = ROOT / "uv.lock"
    pip_lock = ROOT / "requirements.lock.txt"
    if not venv.exists() and not uv_lock.exists() and not pip_lock.exists():
        WARNINGS.append("RT6 未检测到项目 .venv；首次使用请运行 setup.bat 创建隔离环境")


def main() -> int:
    rt1_interpreter()
    rt2_requirements()
    rt3_dbt_cli()
    rt4_versions()
    rt5_launcher_consistency()
    rt6_venv_hint()
    if PROBLEMS:
        print("RUNTIME GATE FAILED (RT1-RT6):")
        for p in PROBLEMS:
            print("  ✗", p)
        for w in WARNINGS:
            print("  !", w)
        return 1
    print("runtime gate: PASS (RT1-RT6 clean)")
    for w in WARNINGS:
        print("  note:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
