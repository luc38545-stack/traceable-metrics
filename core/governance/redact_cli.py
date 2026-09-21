#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D7 敏感数据确定性脱敏 CLI（P2 修复：Skill 交付层强制调用，不再只靠文字规则）。

用法（在主项目根目录）：
    python -m core.governance.redact_cli --rows <rows.json> --level <level> \
        [--policy infra/governance/sensitivity_policy.json] [--out <path>]

- --rows   ：JSON 文件，内容为 [{field: value, ...}, ...]（字段集合必须一致）
- --level  ：交付对象的可见级别（public / internal / confidential / restricted）
- --policy ：显式字段策略（infra/governance/sensitivity_policy.json）；
             省略时用字段名启发式（未知字段默认 internal 级）
- --out    ：省略时写到 <rows>.redacted.json

退出码：0 成功（stdout 输出一行 JSON 摘要）；1 参数/数据错误。
脱敏副本写入 --out；原始文件保持不变（D7：输入不可变）。
Skill 流程约定：任何要交给用户的结构化数据（行数据/导出表）必须先过本 CLI，
拿到 <rows>.redacted.json 后才能进入交付物。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.governance.sensitivity import (
    SensitivityError,
    SensitivityLevel,
    load_policy,
    redact_rows,
)

VALID_LEVELS = [lv.name.lower() for lv in SensitivityLevel]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.governance.redact_cli",
                                 description="D7 确定性脱敏（对外交付边界）")
    ap.add_argument("--rows", required=True, type=Path,
                    help="行数据 JSON 文件：[{field: value}, ...]")
    ap.add_argument("--level", required=True,
                    help=f"交付对象可见级别：{' / '.join(VALID_LEVELS)}")
    ap.add_argument("--policy", type=Path, default=None,
                    help="显式字段策略 JSON（schema_version=1）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    try:
        level = SensitivityLevel.parse(args.level)
    except Exception:  # noqa: BLE001 — 显性报错
        print(json.dumps({"status": "ERROR",
                          "reason": f"--level 必须是 {'/'.join(VALID_LEVELS)}"},
                         ensure_ascii=False))
        return 1
    if not args.rows.exists():
        print(json.dumps({"status": "ERROR", "reason": f"rows 文件不存在：{args.rows}"},
                         ensure_ascii=False))
        return 1
    # D7 铁律：输入不可变。--out 与 --rows 解析后同路径 → 显式拒绝，
    # 绝不允许脱敏输出覆盖原始输入（P2 修复）。
    out = args.out or args.rows.with_suffix(".redacted.json")
    if out.resolve() == args.rows.resolve():
        print(json.dumps({
            "status": "ERROR",
            "reason": f"--out 与 --rows 是同一文件（{args.rows}）——"
                      "脱敏输出禁止覆盖原始输入（D7 输入不可变），请换一个 --out 路径",
        }, ensure_ascii=False))
        return 1
    try:
        rows = json.loads(args.rows.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or (rows and not isinstance(rows[0], dict)):
            raise SensitivityError("rows 必须是 [{field: value}, ...] 数组")
        policy = load_policy(args.policy) if args.policy else None
        redacted = redact_rows(rows, reveal_level=level, policy=policy)
    except (SensitivityError, json.JSONDecodeError) as e:
        print(json.dumps({"status": "ERROR", "reason": str(e)}, ensure_ascii=False))
        return 1

    out.write_text(json.dumps(redacted, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps({
        "status": "OK",
        "out": str(out),
        "reveal_level": level.name.lower(),
        "n_rows": len(redacted),
        "note": "原始文件未改动（D7 输入不可变）；交付物只允许使用本脱敏副本",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
