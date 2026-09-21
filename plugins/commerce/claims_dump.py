# -*- coding: utf-8 -*-
"""claims 对账产物导出 CLI（阶段2 · Skill 依赖接口）。

用法（在主项目根目录）：
    python -m plugins.commerce.claims_dump --run <run_id> [--out <path>]

复用 plugins.commerce.webapp.action_claims 的既有编排（对账逻辑全局唯一，
在 core.claims.reconcile，本模块不做任何对账计算），把 claims 对账结果
落成 JSON 文件，供 D6 独立闸门（core.copilot.gate）消费。

产物默认写到 data/runs/<run_id>.claims.json（与台账同目录）。
对账不通过 / run 不存在 → 退出码 1，显性报错。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m plugins.commerce.claims_dump",
                                 description="导出 run 的 claims 对账产物")
    ap.add_argument("--run", required=True, help="run_id，如 r-20260905-223047-ff0d")
    ap.add_argument("--out", type=Path, default=None,
                    help="输出路径（默认 data/runs/<run_id>.claims.json）")
    args = ap.parse_args(argv)

    # webapp 模块导入即含 CSRF 等常量，无副作用；action_claims 是唯一编排入口
    from plugins.commerce.webapp import action_claims

    try:
        result = action_claims(args.run)
    except Exception as e:  # noqa: BLE001 — 显性报错，不静默
        print(json.dumps({"status": "ERROR", "reason": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 1

    out = args.out or (ROOT / "data" / "runs" / f"{args.run}.claims.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps({
        "status": "OK",
        "out": str(out),
        "reconcile_diff": result.get("reconcile_diff"),
        "n_claims": len(result.get("claims") or []),
    }, ensure_ascii=False))
    if result.get("reconcile_diff") != "clean":
        print("claims 对账未通过：产物已落盘供审计，但 D6 闸门会据此拦截",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
