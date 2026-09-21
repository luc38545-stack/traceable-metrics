"""存储抽象接口（宪法 C-3 的执行点）。

依赖方向图（§06+.2）最底层：semantic/stats/report/claims/audit → storage 抽象接口
(DuckDB 实现注入)。core 内**唯一**允许 import duckdb 的模块；其他 core 模块一律
经本模块取连接（CI R5 静态扫描强制）。调用方（app/插件层）决定连哪个库文件——
core 不知道数据住在哪（C-4）。
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any, Optional

import duckdb


def connect(db_path: Optional[Path] = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """打开一个 DuckDB 连接。db_path 为 None 时是内存库（只读=false）。

    - db_path 由调用方注入（track 卷路径由 app 层决定，core 不感知）。
    - read_only=True 用于快照重放/对账（ADR-18 的 replay 基准只读）。
    """
    return duckdb.connect(str(db_path) if db_path else ":memory:", read_only=read_only)


# ---- 编码探测（架构书 §04 L1：摄取管线首步 = 编码探测）----
# 零新依赖：不引 chardet。gb18030 是 GBK/GB2312 的超集，覆盖 Excel「另存为 CSV」的默认产物。
_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5")


def detect_encoding(path: Path, sample_size: int = 256 * 1024) -> str:
    """按序试探解码，返回第一个可用编码；全部失败则 latin-1 兜底（不丢字节，仅降级可读）。

    真人主旅程（D12）最常见的坑：Windows Excel 导出的 CSV 是 GBK，
    未经探测直接按 UTF-8 读会整表乱码或抛解码错误。
    """
    raw = Path(path).read_bytes()[:sample_size]
    for enc in _CSV_ENCODINGS:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def utf8_view(path: Path) -> Path:
    """返回可供引擎直接读取的 UTF-8 路径；源文件字节一律不动（P3）。

    已是 UTF-8（含 BOM）→ 返回原路径；否则转码副本落系统临时目录，
    按内容 sha256 缓存复用，同一份文件重复体检不会反复产副本。
    """
    p = Path(path)
    enc = detect_encoding(p)
    if enc in ("utf-8", "utf-8-sig"):
        return p
    data = p.read_bytes()
    cache = Path(tempfile.gettempdir()) / "traceable-csv" / hashlib.sha256(data).hexdigest()[:16]
    out = cache / (p.stem + ".utf8.csv")
    if not out.exists():
        cache.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data.decode(enc, errors="replace").encode("utf-8"))
    return out


def read_csv(
    conn: duckdb.DuckDBPyConnection, path: Path, header: bool = True
) -> Any:
    """统一 CSV 读取入口（strict_mode=false：体检/建模容忍脏数据，由体检引擎报问题）。

    编码经 detect_encoding 探测；非 UTF-8 走转码副本，源文件不改（P3）。
    """
    return conn.read_csv(
        str(utf8_view(Path(path))), header=header, sample_size=-1, strict_mode=False
    )
