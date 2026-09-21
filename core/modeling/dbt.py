"""dbt 声明建模运行器（架构书 §04-L3「dbt 声明模型」的共享实现）。

模型目录在仓库 dbt/（dbt_project.yml + profiles.yml + models/），
由 app 层（run_v1 / webapp）程序化调用：
  1. 经 env_var 注入本轨数据库路径（TRACEABLE_DB_PATH）——core 不硬编码 /data/*（C-4）；
  2. raw 批次文件路径经 --vars raw_csv 注入（P3：建模从不可变批次读，不读上传临时文件）。

core 不 import duckdb（R5）；dbt 是声明式建模工具而非存储引擎硬编码。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DBT_DIR = Path(__file__).resolve().parents[2] / "dbt"


class ModelingError(RuntimeError):
    """dbt 建模失败。"""


def run_dbt_model(db_path: Path, raw_csv: Path, select: str = "dwd_base") -> None:
    """跑 dbt run，把指定模型（默认 dwd_base）物化进 db_path（本轨库文件）。

    db_path / raw_csv / select 均由调用方注入（track 参数化，core 不选择数据位置）。

    select 必传（默认 dwd_base）：双轨共用同一 dbt 工程，**不指定 --select 会
    把所有模型跑进当前库**——commerce 轨跑出 dwd_cohort、research 轨跑出 dwd_base
    都属于模型串门。调用方必须显式声明本次要哪个模型（P8' 双轨不互相踩）。

    用 subprocess 而非 dbtRunner.invoke：in-process 时 dbt-duckdb 的连接在
    invoke 返回后仍持有 db 文件句柄（Windows WinError 32），子进程退出即释放。
    """
    if not DBT_DIR.exists():
        raise ModelingError(f"dbt project missing: {DBT_DIR}")

    import shutil
    import subprocess

    dbt_bin = shutil.which("dbt")
    if not dbt_bin:
        raise ModelingError("dbt executable not found on PATH (pip install dbt-duckdb)")

    vars_json = json.dumps({"raw_csv": str(raw_csv)})
    # Windows 中文系统的首选编码通常是 GBK。dbt 解析 UTF-8 工程文件时会沿用
    # Python 默认编码，遇到中文注释便可能在建模前直接 UnicodeDecodeError。
    # 子进程边界固定 UTF-8；同时显式指定父进程如何解码捕获到的输出。
    env = {
        **os.environ,
        "TRACEABLE_DB_PATH": str(db_path),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    # Launch through the repository wrapper.  dbt-core's Windows ThreadPool
    # creates multiprocessing named pipes, which are denied by this managed
    # environment before model execution begins.
    proc = subprocess.run(
        [
            os.fspath(__import__("sys").executable), "-m", "core.modeling.dbt_runner", "run",
            "--project-dir", str(DBT_DIR),
            "--profiles-dir", str(DBT_DIR),
            "--select", select,
            "--vars", vars_json,
            "--log-level", "error",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )
    if proc.returncode != 0:
        # dbt 的关键异常通常写 stdout，启动器错误也可能写 stderr；两边都保留，
        # 避免再次出现只有 "rc=2"、真正原因被静默吞掉的失败提示。
        output = "\n".join(x for x in (proc.stdout, proc.stderr) if x)
        tail = output.strip().splitlines()[-12:]
        raise ModelingError(f"dbt run failed (rc={proc.returncode}): {' | '.join(tail)}")


def assert_model_built(conn, model: str = "dwd_base", min_rows: int = 0) -> int:
    """校验指定模型已物化（建模失败的早期红线）。返回行数。"""
    if not (isinstance(model, str) and model.replace("_", "").isalnum()):
        raise ModelingError(f"非法模型名: {model!r}")
    try:
        n = conn.execute(f'SELECT COUNT(*) FROM "{model}"').fetchone()[0]
    except Exception as e:  # noqa: BLE001
        raise ModelingError(f"{model} not built: {e}") from e
    if n < min_rows:
        raise ModelingError(f"{model} empty (expected >= {min_rows} rows)")
    return n


def assert_dwd_built(conn, min_rows: int = 0) -> int:
    """向后兼容包装：默认检查 dwd_base。"""
    return assert_model_built(conn, model="dwd_base", min_rows=min_rows)
