#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skill 包快速自检（发布前最低门槛）。

校验对象：本 Skill 包自身的一致性，不涉及主项目环境（那是 check_env.py 的职责）：
  Q1 SKILL.md frontmatter：name 与 description 齐备
  Q2 版本一致性：SKILL.md 标题固定的主项目 tag == check_env.py 的 PINNED_RELEASE
  Q3 安装命令：clone 显式目录名 + checkout 固定 tag + 只用 requirements.lock.txt
  Q4 引用完整：SKILL.md 提到的 references/ 文件真实存在
  Q5 无开发者机器路径泄漏（盘符绝对路径）

用法：python quick_validate.py    （退出码 0=通过，1=失败）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
PROBLEMS: list[str] = []


def main() -> int:
    skill_md = SKILL_DIR / "SKILL.md"
    if not skill_md.exists():
        print("FAIL: SKILL.md 不存在")
        return 1
    text = skill_md.read_text(encoding="utf-8")

    # Q1 frontmatter
    m = re.match(r"^---\s*\nname:\s*(\S+)\s*\ndescription:\s*>-?\s*\n(.+?)\n---",
                 text, re.S)
    if not m:
        PROBLEMS.append("Q1 frontmatter 缺 name/description 或格式不符")
    else:
        if m.group(1) != "traceable-analysis":
            PROBLEMS.append(f"Q1 name={m.group(1)!r}，应为 traceable-analysis")
        if len(m.group(2).strip()) < 20:
            PROBLEMS.append("Q1 description 过短")

    # Q2 版本一致性
    pin_env = re.search(r'PINNED_RELEASE\s*=\s*"([^"]+)"',
                        (HERE / "check_env.py").read_text(encoding="utf-8"))
    pin_skill = re.search(r"主项目固定版本\s+(\S+?)[）)]", text)
    if not pin_env:
        PROBLEMS.append("Q2 check_env.py 缺 PINNED_RELEASE 定义")
    if not pin_skill:
        PROBLEMS.append("Q2 SKILL.md 标题缺「主项目固定版本 vX」声明")
    if pin_env and pin_skill and pin_env.group(1) != pin_skill.group(1):
        PROBLEMS.append(
            f"Q2 版本不一致：SKILL.md={pin_skill.group(1)} vs check_env={pin_env.group(1)}")

    # Q3 安装命令
    if not re.search(r"git clone \S+ traceable", text):
        PROBLEMS.append("Q3 clone 命令未显式指定目录名 traceable")
    checkout = re.findall(r"git checkout (\S+)", text)
    if pin_env and pin_env.group(1) not in checkout:
        PROBLEMS.append(f"Q3 checkout 命令未固定到 {pin_env.group(1) if pin_env else 'PIN'}")
    if "pip install -r requirements.txt " in text.replace(".lock.txt ", " ") or \
       re.search(r"pip install -r requirements\.txt\b", text):
        PROBLEMS.append("Q3 安装命令引用了宽松 requirements.txt（必须用 lock）")

    # Q4 references 完整
    for ref in set(re.findall(r"references/([A-Za-z0-9_.\-]+)", text)):
        if not (SKILL_DIR / "references" / ref).exists():
            PROBLEMS.append(f"Q4 SKILL.md 引用的 references/{ref} 不存在")

    # Q5 开发机路径泄漏（跳过本脚本自身——其正则字面量会自我命中）
    scan_targets = [skill_md, *(SKILL_DIR / "references").glob("*.md"),
                    *HERE.glob("*.py")]
    for f in scan_targets:
        if f.resolve() == HERE.resolve() / "quick_validate.py":
            continue
        body = f.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"[Dd]:\\\\[A-Za-z]|C:\\\\Users\\\\|[Dd]:/[A-Za-z]|C:/Users/", body):
            PROBLEMS.append(f"Q5 疑似开发机绝对路径泄漏：{f.name}")

    if PROBLEMS:
        print("QUICK VALIDATE FAILED:")
        for p in PROBLEMS:
            print("  ✗", p)
        return 1
    print("quick_validate: PASS（Q1-Q5 clean）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
