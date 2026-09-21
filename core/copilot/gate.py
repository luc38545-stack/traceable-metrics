# -*- coding: utf-8 -*-
"""D6 数字对账独立闸门 CLI（阶段2 · Skill 依赖接口）。

用法：
    python -m core.copilot.gate --narrative <file> --ledger <run.json> \
        --claims <claims.json> --snapshot <快照文件>

- --narrative 指向的文件既可以是纯文本正文（此时必须给 --citations），
  也可以是 {"narrative": "...", "citations": [...]} 的 JSON（优先）。
- 闸门**复用 core.copilot.claims_gate.gate_narrative**，本模块不实现任何数字校验逻辑。

退出码（机器可读，Skill 据此分流）：
    0  PASS       —— 正文与 citations 全部过闸，允许交付
    1  USAGE      —— 参数错误
    2  GATE_FAIL  —— D6 对账失败（bad_citations / unmatched_numbers，JSON 失败单见 stdout）
    3  BLOCKED    —— 台账/claims 不可信：provenance 四元组缺失、claims 未对账通过、ReportBlocked
    4  SCHEMA     —— 台账 schema_version 缺失或不受支持（显式终止，不猜测版本）

stdout 固定输出一行 JSON 机器可读结果，人读细节走 stderr。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.audit.ledger import LEDGER_SCHEMA_VERSION
from core.copilot.claims_gate import gate_narrative
from core.copilot.explain import build_bank
from core.report.kernel import build_conclusion
from core.report.schema import TRACKS

REQUIRED_PROVENANCE = ("run_id", "snapshot_id", "batch_id", "commit_sha")


def _load_narrative(path: Path) -> tuple[str, list[str]]:
    """narrative 文件：JSON 双形态优先，纯文本回退（citations 由参数给）。"""
    raw = path.read_text(encoding="utf-8")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw, []
    if isinstance(obj, dict) and isinstance(obj.get("narrative"), str):
        return obj["narrative"], list(obj.get("citations") or [])
    return raw, []


def run_gate(
    narrative_path: Path,
    ledger_path: Path,
    claims_path: Path,
    citations_arg: list[str] | None = None,
    snapshot_path: Path | None = None,
) -> tuple[int, dict]:
    """执行闸门，返回 (exit_code, 结果 JSON)。纯函数，便于测试直接调用。

    snapshot_path 必填语义（由 CLI 层强制）：闸门要求对真实快照文件重算
    SHA-256，不接受仅字段级完整性检查（P1 修复）。
    """
    # ① 台账 schema_version：缺失或不受支持 → 显式终止，不猜测
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    sv = ledger.get("schema_version")
    if sv is None:
        return 4, {
            "status": "SCHEMA_MISSING",
            "reason": "台账缺 schema_version（历史台账/未经管道生成），D6 闸门拒绝解读",
        }
    if sv != LEDGER_SCHEMA_VERSION:
        return 4, {
            "status": "SCHEMA_UNSUPPORTED",
            "reason": f"台账 schema_version={sv} 不受支持（本闸门仅支持 "
                      f"{LEDGER_SCHEMA_VERSION}）",
        }

    # ② 台账可信性：track 合法 + provenance 四元组（含与台账本体一致性）+
    #    snapshot 完整性字段 + 快照文件 SHA-256 重算（P1 修复：字段存在≠真实）
    if ledger.get("track") not in TRACKS:
        return 3, {
            "status": "BLOCKED",
            "reason": f"track 缺失或非法：{ledger.get('track')!r}",
        }
    prov = ledger.get("provenance") or {}
    missing = [k for k in REQUIRED_PROVENANCE if not prov.get(k)]
    if missing:
        return 3, {
            "status": "BLOCKED",
            "reason": f"provenance 缺 {missing}——数字不可溯源，拒绝解读",
        }
    # provenance 四元组必须与台账本体一致（防跨 run 拼接）
    prov_mismatch = [k for k in ("run_id", "snapshot_id", "batch_id")
                     if prov.get(k) != ledger.get(k)]
    if prov_mismatch:
        return 3, {
            "status": "BLOCKED",
            "reason": f"provenance.{prov_mismatch} 与台账本体不一致——疑似拼接/篡改，拒绝解读",
        }
    if not ledger.get("snapshot_id") or not ledger.get("snapshot_sha256"):
        return 3, {
            "status": "BLOCKED",
            "reason": "缺 snapshot_id / snapshot_sha256——快照不可验证，拒绝解读",
        }

    # ③ 快照文件 SHA-256 重算：报告所称 snapshot 必须与磁盘上的真实文件一致
    import hashlib
    if snapshot_path is None:
        return 3, {
            "status": "BLOCKED",
            "reason": "未提供 --snapshot 快照文件路径——闸门要求重算 SHA-256，"
                      "不接受仅字段级完整性检查",
        }
    if not snapshot_path.exists():
        return 3, {
            "status": "BLOCKED",
            "reason": f"快照文件不存在：{snapshot_path}",
        }
    digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    if digest != ledger.get("snapshot_sha256"):
        return 3, {
            "status": "BLOCKED",
            "reason": f"快照 SHA-256 不一致：台账声明 {str(ledger.get('snapshot_sha256'))[:12]}…，"
                      f"实际 {digest[:12]}…——快照被篡改或错配，拒绝解读",
        }

    # ④ claims 必须对账通过 + source 与台账绑定（防跨 run 混用）
    claims_result = json.loads(claims_path.read_text(encoding="utf-8"))
    for i, cl in enumerate(claims_result.get("claims") or []):
        src = cl.get("source") or {}
        bind_mismatch = [k for k in ("run_id", "snapshot_id", "query_id")
                         if src.get(k) != ledger.get(k)]
        if bind_mismatch:
            return 3, {
                "status": "BLOCKED",
                "reason": f"claims[{i}].source.{bind_mismatch} 与台账不一致"
                          f"——跨 run 混用，拒绝解读",
            }
    try:
        conclusion = build_conclusion(ledger, claims_result)
    except Exception as e:  # ReportBlocked 等：显性拦截
        return 3, {"status": "BLOCKED", "reason": f"{type(e).__name__}: {e}"}

    bank = build_bank(conclusion)
    if not bank:
        return 3, {"status": "BLOCKED", "reason": "结论没有 claims：无对账依据（C-5）"}

    # ⑤ narrative + citations → 复用现有 gate_narrative（禁止另实现数字校验）
    #    strict 语义：正文数字必须落在被引用 claim 的值上，citations=[] 必 FAIL
    narrative, citations = _load_narrative(narrative_path)
    if citations_arg:
        citations = list(citations_arg)
    if not narrative.strip():
        return 1, {"status": "USAGE", "reason": "narrative 文件为空"}
    gate = gate_narrative(narrative, citations, bank, strict=True)
    gate["run_id"] = ledger.get("run_id")
    gate["citations"] = citations
    if gate["status"] != "PASS":
        return 2, gate
    return 0, gate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m core.copilot.gate",
                                 description="D6 数字对账独立闸门")
    ap.add_argument("--narrative", required=True, type=Path,
                    help="解释正文文件（纯文本或 {narrative, citations} JSON）")
    ap.add_argument("--ledger", required=True, type=Path,
                    help="run 台账 JSON（data/runs/r-*.json）")
    ap.add_argument("--claims", required=True, type=Path,
                    help="对账产物 JSON（claims_dump 输出）")
    ap.add_argument("--citations", nargs="+", default=None,
                    help="纯文本正文时的引用 key 列表（metric#period）")
    ap.add_argument("--snapshot", required=True, type=Path,
                    help="本次 run 的快照文件路径（data/<track>/snapshots/<sid>/<track>.db），"
                         "用于重算 SHA-256 对账")
    args = ap.parse_args(argv)

    if not args.narrative.exists():
        print(json.dumps({"status": "USAGE", "reason": "narrative 文件不存在"},
                         ensure_ascii=False))
        return 1
    if not args.ledger.exists() or not args.claims.exists():
        print(json.dumps({"status": "USAGE", "reason": "ledger/claims 文件不存在"},
                         ensure_ascii=False))
        return 1

    code, result = run_gate(args.narrative, args.ledger, args.claims,
                            args.citations, args.snapshot)
    print(json.dumps(result, ensure_ascii=False))
    if code != 0:
        print(f"D6 GATE: {result.get('status')} —— 正文不得交付", file=sys.stderr)
    else:
        print("D6 GATE: PASS", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
