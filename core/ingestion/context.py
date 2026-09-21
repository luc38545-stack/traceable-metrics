"""track context 注入器 —— 宪法 C-4 的运行时执行者。

core 永远不知道数据住在哪：调用方必须显式传入 TrackContext。
core 中不存在任何 /data/commerce、/data/research 字面量（CI R3 强制）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TrackContext:
    """一条轨的运行时身份。volume 根由调用方（app 层）注入。"""

    track: str                  # "commerce" | "research"
    volume_root: Path           # 本轨数据卷根（app 层挂载点）
    project_id: str = "default" # R13 预留维度
    _meta: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.track not in ("commerce", "research"):
            raise ValueError(f"invalid track: {self.track!r}")
        if ".." in str(self.volume_root):
            raise ValueError("volume_root must not contain traversal segments")

    @property
    def raw_dir(self) -> Path:
        return self.volume_root / "raw"

    @property
    def snapshots_dir(self) -> Path:
        return self.volume_root / "snapshots"

    @property
    def db_path(self) -> Path:
        return self.volume_root / f"{self.track}.db"

    def assert_inside_volume(self, p: Path) -> Path:
        """路径必须落在本轨卷内 —— 跨轨访问在此处直接炸（不是被策略拒绝）。"""
        resolved = p.resolve()
        root_resolved = self.volume_root.resolve()
        if root_resolved not in resolved.parents and resolved != root_resolved:
            raise PermissionError(
                f"path escape blocked: {resolved} is outside volume {root_resolved}"
            )
        return resolved
