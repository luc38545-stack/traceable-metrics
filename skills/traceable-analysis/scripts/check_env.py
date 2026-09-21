#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""traceable-analysis Skill 环境自检（失败即显性退出，供 Agent 第 0 步调用）。

用法：
    python check_env.py [--traceable-home PATH]

输出：stdout 单行 JSON（机器可读），退出码 0=就绪 / 1=未就绪。
检查项：
    1. TRACEABLE_HOME 解析（参数 > 环境变量），禁止预设本机路径
    2. 主项目完整性（关键文件存在）
    3. 版本固定（HEAD 含本 Skill 声明的 tag；非 git 目录或脏工作树均失败）
    4. 主项目 .venv 存在且其 Python 能导入运行时依赖
    5. 主项目声明支持的台账 schema 版本
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

#: 本 Skill 支持的主项目版本（升级时同步修改 SKILL.md 顶部声明）
PINNED_RELEASE = "traceable-v1.0.4"
#: 运行时依赖（与主项目 requirements.in 一致）
RUNTIME_DEPS = ("duckdb", "yaml", "dbt")


def _check(name: str, ok: bool, detail: str, checks: list) -> bool:
    checks.append({"name": name, "ok": ok, "detail": detail})
    return ok


def _venv_python(home: Path) -> Path | None:
    scripts = home / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    for cand in ("python.exe", "python"):
        p = scripts / cand
        if p.exists():
            return p
    return None


def _git_worktree_changes(home: Path) -> tuple[bool, list[str]]:
    """返回 Git 状态是否可读及全部未忽略的工作树变更。"""
    result = subprocess.run(
        [
            "git", "-C", str(home), "status", "--porcelain",
            "--untracked-files=all",
        ],
        capture_output=True,
        text=True,
    )
    warning = result.stderr.strip()
    if result.returncode != 0 or warning:
        detail = warning or result.stdout.strip()
        return False, [f"<git status exit {result.returncode}: {detail or '未知错误'}>"]
    return True, [line for line in result.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TraceableMetrics Skill 环境自检")
    ap.add_argument("--traceable-home", type=Path, default=None)
    args = ap.parse_args(argv)

    checks: list[dict] = []
    ok_all = True

    # 1. 主项目位置解析
    home = args.traceable_home or os.environ.get("TRACEABLE_HOME")
    if not home:
        ok_all &= _check(
            "traceable_home", False,
            "未提供 --traceable-home，且环境变量 TRACEABLE_HOME 未设置。"
            "请先 clone 主项目并 checkout " + PINNED_RELEASE, checks)
        home = None
    else:
        home = Path(home).resolve()
        ok_all &= _check("traceable_home", True, str(home), checks)

    # 2. 主项目完整性
    if home is not None:
        required = [
            "core/copilot/gate.py",
            "core/copilot/claims_gate.py",
            "plugins/commerce/run_v1.py",
            "plugins/commerce/claims_dump.py",
            "requirements.lock.txt",
            "schemas/metrics/commerce/pay_success_rate.yml",
        ]
        missing = [r for r in required if not (home / r).exists()]
        ok_all &= _check(
            "project_layout", not missing,
            "关键文件齐全" if not missing else f"缺失：{missing}", checks)

        # 3. 版本固定（硬规则）：必须是 git 仓库、HEAD 确切存在 pinned tag、
        #    工作树干净。非 git 目录（如 ZIP 解压）直接失败——无法证明版本（P1 修复）
        if not (home / ".git").exists():
            ok_all &= _check(
                "version_pin", False,
                "主项目不是 git 仓库（无法证明版本）。请 git clone 并 "
                f"git checkout {PINNED_RELEASE}，不要用 ZIP 解压", checks)
        else:
            try:
                r = subprocess.run(
                    ["git", "-C", str(home), "tag", "--points-at", "HEAD"],
                    capture_output=True, text=True,
                )
                tags = [t.strip() for t in r.stdout.splitlines() if t.strip()]
                if r.returncode == 0 and PINNED_RELEASE in tags:
                    _check("version_pin_tag", True,
                           f"HEAD 位于 {PINNED_RELEASE}（tags: {', '.join(tags)}）", checks)
                else:
                    ok_all &= _check(
                        "version_pin_tag", False,
                        f"HEAD tags={tags or '无'}，缺 {PINNED_RELEASE}。"
                        f"请执行：git checkout {PINNED_RELEASE}", checks)
                # 工作树必须干净：已跟踪改动和未跟踪源码/配置都会改变实际行为。
                # 测试及运行产物只允许通过 .gitignore 的明确规则排除。
                status_ok, changed = _git_worktree_changes(home)
                if status_ok and not changed:
                    _check("worktree_clean", True, "工作树干净", checks)
                else:
                    changed_preview = (changed or ["<git 异常>"])[:3]
                    ok_all &= _check(
                        "worktree_clean", False,
                        f"工作树不干净（{changed_preview}…）——修改过的代码不能再声称是 "
                        f"{PINNED_RELEASE}。请 stash/commit 后重试", checks)
            except OSError as e:
                ok_all &= _check("version_pin_tag", False, f"git 不可用：{e}", checks)

        # 4. .venv 与依赖（锁文件要求 Python >= 3.12：numpy/scipy >=3.12、pandas >=3.11）
        vpy = _venv_python(home)
        if vpy is None:
            ok_all &= _check(
                "venv", False,
                "主项目缺 .venv。请在主项目根目录执行：\n"
                "  Windows: python -m venv .venv && .venv\\Scripts\\python.exe -m pip install -r requirements.lock.txt\n"
                "  Linux/macOS: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.lock.txt\n"
                "（禁止裸 pip——会装进系统 Python，随后 .venv 依赖检查必然失败）", checks)
        else:
            _check("venv", True, str(vpy), checks)
            r = subprocess.run(
                [str(vpy), "-c",
                 "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True)
            ver = r.stdout.strip()
            try:
                ver_ok = tuple(int(x) for x in ver.split(".")) >= (3, 12)
            except ValueError:
                ver_ok = False
            ok_all &= _check(
                "python_version", r.returncode == 0 and ver_ok,
                f"venv Python {ver or '未知'}（要求 >= 3.12，锁文件约束）", checks)
            code = "import " + ", ".join(RUNTIME_DEPS)
            r = subprocess.run([str(vpy), "-c", code], capture_output=True, text=True)
            ok_all &= _check(
                "runtime_deps", r.returncode == 0,
                "运行时依赖可导入" if r.returncode == 0
                else f"依赖导入失败：{r.stderr.strip().splitlines()[-1] if r.stderr else '?'}",
                checks)
            # 台账 schema 版本
            r2 = subprocess.run(
                [str(vpy), "-c",
                 "from core.audit.ledger import LEDGER_SCHEMA_VERSION as v; print(v)"],
                capture_output=True, text=True, cwd=str(home),
            )
            sv = r2.stdout.strip()
            ok_all &= _check(
                "ledger_schema", r2.returncode == 0 and sv == "1",
                f"schema_version={sv or '未知'}（本 Skill 仅支持 1）", checks)

    out = {
        "ok": ok_all,
        "pinned_release": PINNED_RELEASE,
        "traceable_home": str(home) if home else None,
        "checks": checks,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
