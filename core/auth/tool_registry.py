"""Capability Matrix / tool registry（附录 T 冻结项⑦ · §06+.1）。

架构书要求：
- 权限 schema + tool registry 授权器：任何「Agent 能不能调这个函数」以能力矩阵表为准
- 负向：L2 key 尝试白名单外动作 → 拒绝 + 审计
- ADR-17 L2：动作白名单绑定到每把 API 密钥（API 看板逐 key 显示允许动作）
  —— per-key 白名单是 S2 出口判据（Agent L2 首验），越界一律回落人审并审计。

能力矩阵（来自 §06+.1 表，V1 只落 Agent 相关动作子集）：
  Agent L1 辅助：read_via_semantic（受限查询翻译）
  Agent L2 执行：read_via_semantic + pipeline_rerun（白名单流水线内）
  Agent L3 编排：read_via_semantic + pipeline_rerun（默认挂起，不授予新动作）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# 合法轨道（P1-04 ②）：只有这两个值，注册时先拒绝非法值
VALID_TRACKS = ("commerce", "research")

# 每级 Agent 允许的动作白名单（授权 schema 来源 = 能力矩阵）
WHITELIST: dict[str, set[str]] = {
    "L1": {"read_via_semantic"},
    "L2": {"read_via_semantic", "pipeline_rerun"},
    "L3": {"read_via_semantic", "pipeline_rerun"},  # 编排级默认挂起：不新增动作
}

# per-key 白名单（ADR-17 L2 / S2 判据）：key_id → 授权对象。
# 审计 #8 补强：必须同时绑定动作和轨道——key 注册时声明可用轨道，
# 白名单外动作 **或** 白名单外轨道 → 一律拒绝（只查动作不查轨道 = 跨轨越权）。
# P1-04 ①：授权对象扩展 allowed_project_ids / agent_level / expires_at / revoked。
# 注册即覆盖：同一把 key 的授权以最近一次 register_key 为准；
# 未注册的 key 尝试任何动作 → 一律拒绝（默认白名单为空）。
KEY_WHITELIST: dict[str, dict] = {}


class PermissionDenied(PermissionError):
    """越界动作被拒。"""


def register_key(
    key_id: str,
    actions: set[str],
    tracks: set[str],
    project_ids: set[str] | None = None,
    agent_level: str | None = None,
    expires_at: datetime | str | None = None,
    revoked: bool = False,
) -> None:
    """把一把 API 密钥绑定到「动作 × 轨道 × project」白名单。

    - actions / tracks 必填（P4：不做静默的"不限轨道"兜底，那是审计 #8 的漏洞形态）；
    - track 只能是 commerce|research，非法值先拒绝（P1-04 ②）；
    - project_ids 声明后即强制生效：未声明 = 不限 project（V1 单项目），
      声明后 authorize 必须带 project_id 且命中才放行；
    - expires_at 接受 datetime 或 ISO 字符串；revoked=True 立即失效。
    """
    if not actions:
        raise ValueError(f"register_key requires at least one action (key={key_id})")
    if not tracks:
        raise ValueError(
            f"register_key requires at least one track (key={key_id})——"
            f"per-key 白名单必须绑定轨道（审计 #8）"
        )
    bad_tracks = [t for t in tracks if t not in VALID_TRACKS]
    if bad_tracks:
        raise ValueError(
            f"register_key 轨道非法：{bad_tracks}（P1-04：只能 {VALID_TRACKS}，先拒绝）"
        )
    if agent_level is not None and agent_level not in WHITELIST:
        raise ValueError(f"非法 agent_level: {agent_level!r}（只能是 {sorted(WHITELIST)}）")
    KEY_WHITELIST[key_id] = {
        "actions": set(actions),
        "tracks": set(tracks),
        "project_ids": None if project_ids is None else set(project_ids),
        "agent_level": agent_level,
        "expires_at": expires_at,
        "revoked": bool(revoked),
    }


def revoke_key(key_id: str) -> None:
    """吊销一把 key：白名单保留但标记 revoked，之后任何动作都被拒。

    P1-04 ①：撤销状态必须可审计——不是删记录，而是留吊销标记。
    """
    entry = KEY_WHITELIST.get(key_id)
    if entry is not None:
        entry["revoked"] = True


def _parse_expiry(expires_at) -> datetime:
    if isinstance(expires_at, datetime):
        return expires_at
    return datetime.fromisoformat(str(expires_at))


@dataclass
class ToolCall:
    agent_level: str
    action: str
    track: str
    key_id: str | None = None
    allowed: bool = False
    audit_note: str = ""


class ToolRegistry:
    """按能力矩阵 + per-key 白名单授权工具调用；越界动作一律拒绝并落审计说明。"""

    def __init__(self, audit_sink: list[ToolCall] | None = None) -> None:
        # 注意：不能用 field(default_factory=...)——本类不是 dataclass，
        # 那样会把 self._audit 赋成 Field 对象导致 .append 崩（N16 暴露的既有 bug）。
        self._audit: list[ToolCall] = [] if audit_sink is None else audit_sink

    def authorize(self, agent_level: str, action: str, track: str,
                  key_id: str | None = None, project_id: str | None = None) -> ToolCall:
        # P1-04 ③：L3 在 research 强制禁用（编排级动作在论文轨一律挂起）——
        # 即使 key 白名单里写了 research 也拒绝，硬规则优先于任何 key 配置。
        l3_research_blocked = agent_level == "L3" and track == "research"

        # 级别白名单（能力矩阵）+ per-key 白名单（ADR-17 L2）双重校验。
        # per-key 校验（审计 #8 + P1-04）：动作/轨道/project/有效期/撤销全部在
        # 白名单内才放行；key 未注册、任一维度越界 → 拒绝。
        allowed = action in WHITELIST.get(agent_level, set()) and not l3_research_blocked
        if allowed and key_id is not None:
            entry = KEY_WHITELIST.get(key_id)
            allowed = (
                entry is not None
                and action in entry["actions"]
                and track in entry["tracks"]
                and not entry.get("revoked", False)
            )
            exp = entry.get("expires_at") if entry else None
            if allowed and exp is not None:
                try:
                    allowed = datetime.now() < _parse_expiry(exp)
                except (ValueError, TypeError):
                    allowed = False        # 无法解析的有效期 → 视为不可用
            projs = entry.get("project_ids") if entry else None
            if allowed and projs is not None:
                # 声明了 project_ids：必须带 project_id 且命中（无法证明作用域 = 拒绝）
                allowed = project_id is not None and project_id in projs
        call = ToolCall(
            agent_level=agent_level,
            action=action,
            track=track,
            key_id=key_id,
            allowed=allowed,
            audit_note=(
                "allowed"
                if allowed
                else f"DENIED: out-of-whitelist (level={agent_level}, key={key_id or '-'}, "
                     f"track={track!r}, project={project_id!r})"
            ),
        )
        self._audit.append(call)  # 每次调用都进审计（D8）
        if not allowed:
            raise PermissionDenied(
                f"{agent_level} action {action!r} denied on track {track!r} "
                f"(key={key_id or 'none'}; out-of-whitelist, see audit)"
            )
        return call
