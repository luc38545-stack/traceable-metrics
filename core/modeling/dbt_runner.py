"""Windows-safe dbt CLI launcher.

dbt-core 1.12 builds its ``ThreadPool`` through ``multiprocessing.Pool``.
On some managed Windows environments that creates a named pipe and fails with
WinError 5 before any model SQL runs.  This launcher keeps dbt's graph and
adapter behavior unchanged while providing the small async-pool API dbt uses,
backed by ``concurrent.futures.ThreadPoolExecutor``.
"""
from __future__ import annotations

import multiprocessing.pool
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class _AsyncResult:
    def __init__(self, future):
        self._future = future

    def get(self, timeout: float | None = None):
        return self._future.result(timeout)

    def wait(self, timeout: float | None = None) -> None:
        self._future.result(timeout)


class _ThreadPool:
    """Minimal multiprocessing.pool.ThreadPool-compatible adapter for dbt."""

    def __init__(self, processes: int, initializer=None, initargs=()):
        self._executor = ThreadPoolExecutor(
            max_workers=processes,
            initializer=initializer,
            initargs=tuple(initargs),
        )

    def apply_async(
        self,
        func: Callable[..., Any],
        args=(),
        kwds=None,
        callback=None,
        error_callback=None,
    ) -> _AsyncResult:
        future = self._executor.submit(func, *args, **(kwds or {}))

        def done(f):
            try:
                result = f.result()
            except BaseException as exc:  # pragma: no cover - dbt handles worker errors
                if error_callback:
                    error_callback(exc)
                return
            if callback:
                callback(result)

        future.add_done_callback(done)
        return _AsyncResult(future)

    def close(self) -> None:
        # dbt calls terminate/join after close; defer shutdown to join so all
        # already submitted model callbacks can complete normally.
        return None

    def terminate(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def join(self) -> None:
        self._executor.shutdown(wait=True)


def main() -> int:
    multiprocessing.pool.ThreadPool = _ThreadPool
    from dbt.cli.main import cli

    return int(cli())


if __name__ == "__main__":
    sys.exit(main())
