"""跨轨导出桥（宪法 C-4：跨轨数据流动仅导出桥一条边 · S4 双轨合龙）。

共享核心，不 import plugins、不硬编码任何 /data/* 路径（C-4 / R3）：
两侧卷、中转目录、台账路径全部由 app 层（face=A 控制面）注入。
"""
from core.bridge.bridge import (
    BridgeGateError,
    TransferToken,
    export_snapshot,
    issue_transfer_token,
    redeem_transfer_token,
)

__all__ = [
    "BridgeGateError",
    "TransferToken",
    "issue_transfer_token",
    "redeem_transfer_token",
    "export_snapshot",
]
