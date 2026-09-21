#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4 导出桥五闸测试（架构书 §六 S4 · 宪法 C-4「跨轨数据流动仅导出桥一条边」）。

对照修改意见 P1-01（源轨出口网关 / 受控中转 / 目标轨入口网关三段式）、
P1-04（导出桥只能由人审控制面触发）、ADR-15（15 分钟会话）、
D8（跨轨桥动作进 append-only 审计流）逐闸落测：

  B1-B4   闸1 人审 token：无/伪造/过期/已用/purpose 不符 → 拒绝
  B5-B7   闸2 源轨出口：伪造 run_id / 快照缺失 / sha256 不符 → 拒绝
  B8      闸3 受控中转：中转目录落在轨卷内 → 拒绝（防绕过卷守卫）
  B9-B10  闸4 目标轨入口：越界写 / 复制复核失败 → 拒绝且不留半成品
  B11     闸5 双端审计：审计失败 → 拒绝交付（先审计后交付）
  B12     正向全流程：人审 token + 真实 run + 快照 → 导出成功，
          双端事件落账、manifest 完整、目标 imports 文件 sha256 一致
  B13-B14 反向路径不存在性：目标轨 ctx 读源轨卷路径 / 源轨 ctx 读目标轨 → PermissionError

运行：仓库根目录下  pytest -q tests/test_bridge.py
"""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from core.audit import ledger
from core.bridge import (
    BridgeGateError,
    TransferToken,
    export_snapshot,
    issue_transfer_token,
    redeem_transfer_token,
)
from core.ingestion.context import TrackContext


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def env(tmp_path: Path):
    """构造双轨卷 + 受控中转 + 源台账 run + 快照。

    source 卷：ledger.db（一条真实 run，快照文件 sha256 与台账一致）
    target 卷：空卷（导入区在 imports/）
    bridge_dir：非轨卷受控中转（tmp_path/bridge）
    """
    src_vol = tmp_path / "v_commerce"
    tgt_vol = tmp_path / "v_research"
    bridge = tmp_path / "bridge"
    for v in (src_vol, tgt_vol, bridge):
        v.mkdir(parents=True, exist_ok=True)

    src_ctx = TrackContext(track="commerce", volume_root=src_vol)
    tgt_ctx = TrackContext(track="research", volume_root=tgt_vol)

    # 快照文件（假 db 内容即可——导出桥只做文件级完整性校验）
    snap_dir = src_ctx.snapshots_dir
    snap_dir.mkdir(parents=True)
    snap_file = snap_dir / "s-run-test-0001"
    snap_file.write_bytes(b"TRACEABLE-SNAPSHOT-" + b"\x00" * 128)
    snap_sha = hashlib.sha256(snap_file.read_bytes()).hexdigest()

    # 源台账：一条真实 run（provenance 四元组 + snapshot_sha256 双层记录）
    src_ledger = src_vol / "ledger.db"
    ledger.write_run(src_ledger, {
        "run_id": "r-run-test-0001",
        "track": "commerce",
        "started_at": "2026-08-31T10:00:00",
        "batch_id": "b-batch-001",
        "snapshot_id": "s-run-test-0001",
        "snapshot_sha256": snap_sha,
        "rows": 42,
        "metric": "pay_success_rate",
        "metric_version": 1,
        "health_summary": {"rows": 42},
        "provenance": {
            "run_id": "r-run-test-0001",
            "snapshot_id": "s-run-test-0001",
            "batch_id": "b-batch-001",
            "commit_sha": "0" * 64,
            "snapshot_sha256": snap_sha,
        },
        "steps": ["ok"],
    })
    # 目标台账（独立空卷台账）
    tgt_ledger = tgt_vol / "ledger.db"
    return {
        "src_ctx": src_ctx, "tgt_ctx": tgt_ctx, "bridge": bridge,
        "src_ledger": src_ledger, "tgt_ledger": tgt_ledger,
        "snap_sha": snap_sha, "run_id": "r-run-test-0001",
    }


def _issue(env, purpose: str = "commerce→research") -> str:
    tok = issue_transfer_token(
        env["bridge"], purpose=purpose,
        source_track="commerce", target_track="research")
    return tok.token_id


def _export(env, token_id: str, run_id: str | None = None, purpose: str = "commerce→research",
            **kw):
    return export_snapshot(
        token_id=token_id, purpose=purpose,
        source_ctx=env["src_ctx"], target_ctx=env["tgt_ctx"],
        bridge_dir=env["bridge"],
        source_ledger_db=env["src_ledger"], target_ledger_db=env["tgt_ledger"],
        run_id=run_id or env["run_id"], **kw)


# ---------------------------------------------------------------- 闸1：人审 token

def test_b1_no_token_rejected(env):
    """闸1：无人审 token → 拒绝（导出桥只能由 face=A 控制面触发）。"""
    with pytest.raises(BridgeGateError, match="人审 token"):
        _export(env, token_id="")


def test_b2_forged_token_rejected(env):
    """闸1：伪造 token → 拒绝。"""
    with pytest.raises(BridgeGateError, match="无效或不存在"):
        _export(env, token_id="tk-forged-000000")


def test_b3_expired_token_rejected(env):
    """闸1：过期 token → 拒绝（ADR-15 会话超时语义）。"""
    import datetime as dt

    p = env["bridge"] / "tokens"
    p.mkdir(parents=True, exist_ok=True)
    past = (dt.datetime.now() - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    tok = TransferToken(token_id="tk-expired", purpose="commerce→research",
                        source_track="commerce", target_track="research",
                        issued_at=past, expires_at=past, used=False)
    (p / "tk-expired.json").write_text(
        json.dumps(tok.to_dict(), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BridgeGateError, match="已过期"):
        _export(env, token_id="tk-expired")


def test_b4_token_single_use(env):
    """闸1：token 一次性——二次使用 → 拒绝（禁止重放）。"""
    tok_id = _issue(env)
    first = _export(env, tok_id)
    assert first["export_id"]
    with pytest.raises(BridgeGateError, match="已使用"):
        _export(env, tok_id)


def test_b13_token_purpose_mismatch_rejected(env):
    """闸1：token purpose 与请求不一致 → 拒绝（人审批准与请求必须一致）。"""
    tok_id = _issue(env, purpose="commerce→research")
    with pytest.raises(BridgeGateError, match="purpose 不符"):
        _export(env, tok_id, purpose="research→commerce")


# ---------------------------------------------------------------- 闸2：源轨出口

def test_b5_forged_run_id_rejected(env):
    """闸2：源台账无此 run → 拒绝（禁止导出未登记产物）。"""
    tok_id = _issue(env)
    with pytest.raises(BridgeGateError, match="无此 run"):
        _export(env, tok_id, run_id="r-nonexistent")


def test_b6_snapshot_missing_rejected(env):
    """闸2：台账有快照记录但文件缺失 → 拒绝（不许导出残缺产物）。"""
    tok_id = _issue(env)
    snap = env["src_ctx"].snapshots_dir / "s-run-test-0001"
    snap.unlink()
    with pytest.raises(BridgeGateError, match="快照缺失"):
        _export(env, tok_id)


def test_b7_snapshot_tampered_rejected(env):
    """闸2：快照内容被改 → sha256 与台账不符 → 拒绝（D3 溯源不可伪造）。"""
    tok_id = _issue(env)
    snap = env["src_ctx"].snapshots_dir / "s-run-test-0001"
    snap.write_bytes(b"TAMPERED-" + b"\x00" * 64)
    with pytest.raises(BridgeGateError, match="sha256 与台账不一致"):
        _export(env, tok_id)


# ---------------------------------------------------------------- 闸3：受控中转

def test_b8_bridge_dir_inside_volume_rejected(env):
    """闸3：受控中转落在源轨卷内 → 拒绝（防绕过卷守卫）。"""
    tok_id = _issue(env)
    bad_bridge = env["src_ctx"].volume_root / "staging"   # 源轨卷内
    # token 文件复制到「轨卷内 bridge」的 tokens/ 下，让闸1 先过、闸3 拒绝
    (bad_bridge / "tokens").mkdir(parents=True, exist_ok=True)
    src_tok = env["bridge"] / "tokens" / f"{tok_id}.json"
    (bad_bridge / "tokens" / f"{tok_id}.json").write_bytes(src_tok.read_bytes())
    with pytest.raises(BridgeGateError, match="不得位于源轨卷内"):
        export_snapshot(
            token_id=tok_id, purpose="commerce→research",
            source_ctx=env["src_ctx"], target_ctx=env["tgt_ctx"],
            bridge_dir=bad_bridge,
            source_ledger_db=env["src_ledger"], target_ledger_db=env["tgt_ledger"],
            run_id=env["run_id"])


# ---------------------------------------------------------------- 闸4：目标轨入口

def test_b9_target_volume_guard(env):
    """闸4：目标轨卷守卫——越界写（源轨路径）→ PermissionError（反向路径不存在）。"""
    with pytest.raises(PermissionError):
        env["tgt_ctx"].assert_inside_volume(env["src_ctx"].volume_root / "x.db")


def test_b14_reverse_path_both_directions(env):
    """反向路径不存在性（双向）：目标轨读源轨卷 / 源轨读目标轨 → PermissionError。"""
    with pytest.raises(PermissionError):
        env["tgt_ctx"].assert_inside_volume(env["src_ctx"].volume_root / "raw" / "a.csv")
    with pytest.raises(PermissionError):
        env["src_ctx"].assert_inside_volume(env["tgt_ctx"].volume_root / "raw" / "b.csv")
    # 本轨路径放行
    ok = env["src_ctx"].assert_inside_volume(env["src_ctx"].volume_root / "raw" / "a.csv")
    assert ok.name == "a.csv"


# ---------------------------------------------------------------- 闸5：双端审计

def test_b11_audit_failure_aborts(env):
    """闸5：目标台账不可写 → 审计失败 → 拒绝交付且回滚（先审计后交付）。"""
    tok_id = _issue(env)
    # 让目标台账路径不可写：父路径指向一个已存在文件 → _conn mkdir 失败
    bad_ledger = env["tgt_ctx"].volume_root / "ledger.db"
    bad_ledger.write_text("I AM A FILE NOT A DIR", encoding="utf-8")
    blocker = bad_ledger / "sub" / "ledger.db"   # 父路径是文件 → OSError
    with pytest.raises(BridgeGateError, match="审计失败"):
        export_snapshot(
            token_id=tok_id, purpose="commerce→research",
            source_ctx=env["src_ctx"], target_ctx=env["tgt_ctx"],
            bridge_dir=env["bridge"],
            source_ledger_db=env["src_ledger"], target_ledger_db=blocker,
            run_id=env["run_id"])
    # 回滚：目标 imports 不留半成品
    imports = env["tgt_ctx"].volume_root / "imports"
    leftovers = list(imports.glob("*")) if imports.exists() else []
    assert not leftovers, f"审计失败必须回滚目标卷，遗留: {leftovers}"


# ---------------------------------------------------------------- 正向全流程

def test_b12_export_success_full_flow(env):
    """正向：人审 token + 真实 run + 快照 → 五闸全过，双端审计 + manifest + sha 一致。"""
    tok_id = _issue(env)
    out = _export(env, tok_id, claims=[{"metric": "pay_success_rate", "value": 0.875}])

    m = out["manifest"]
    assert out["imported"].endswith("imports" + "\\" + "run-test-0001.snapshot.db") or \
        out["imported"].endswith("imports/run-test-0001.snapshot.db")
    # 目标落卷文件 sha256 与源快照一致
    imported = Path(out["imported"])
    assert hashlib.sha256(imported.read_bytes()).hexdigest() == env["snap_sha"]
    # manifest 完整：source 四元组 + file sha + claims 摘要（ADR-18）
    assert m["source"]["track"] == "commerce"
    assert m["source"]["run_id"] == env["run_id"]
    assert m["source"]["snapshot_id"] == "s-run-test-0001"
    assert m["source"]["batch_id"] == "b-batch-001"
    assert m["source"]["commit_sha"]
    assert m["file"]["sha256"] == env["snap_sha"]
    assert m["claims_count"] == 1 and m["claims"][0]["metric"] == "pay_success_rate"
    assert m["approved_by"] == "face=A 人审控制面"
    assert Path(out["manifest_path"]).exists()

    # 闸5：双端事件落账（D8：跨轨桥全覆盖）
    def _events(db_path: Path) -> list[str]:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute(
                "SELECT event_type FROM events WHERE event_type='export_bridge'")]
        finally:
            conn.close()

    assert "export_bridge" in _events(env["src_ledger"]), "源轨必须记录导出事件"
    assert "export_bridge" in _events(env["tgt_ledger"]), "目标轨必须记录导入事件"


def test_b15_redeem_api_direct(env):
    """闸1 API 直测：redeem 校验轨向 + 一次性语义（直接调用层面）。"""
    tok = issue_transfer_token(env["bridge"], purpose="commerce→research",
                               source_track="commerce", target_track="research")
    redeemed = redeem_transfer_token(env["bridge"], tok.token_id,
                                     purpose="commerce→research",
                                     source_track="commerce", target_track="research")
    assert redeemed.used is True, "redeem 后必须标记 used"
    with pytest.raises(BridgeGateError, match="已使用"):
        redeem_transfer_token(env["bridge"], tok.token_id,
                              purpose="commerce→research",
                              source_track="commerce", target_track="research")
    with pytest.raises(BridgeGateError, match="非法源轨"):
        issue_transfer_token(env["bridge"], purpose="x",
                             source_track="mars", target_track="research")
