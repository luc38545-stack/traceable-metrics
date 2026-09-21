"""P2-01：把「脚本式自测」升级为标准 pytest 用例的最小工具集。

背景：原来每个测试文件都是脚本——用例在模块导入期就跑完，末尾 `sys.exit()`。
这样 pytest 一导入就 SystemExit，整条收集链崩掉（INTERNALERROR），
等于「测试存在」但「测试不可用」。

统一形态：每个测试文件只维护三张声明式用例表——

- `CASES`：必须成立的用例 `(名称, 返回真值的函数)`；
- `REJECT_CASES`：必须被拒的用例 `(名称, 期望异常类型, 函数)`；
- `FAILMSG_CASES`：必须被拒且错误信息要点名关键词的用例
  `(名称, 函数, 错误信息里必须出现的片段)`。

由本模块同时供给两种运行方式：
- `pytest -q`（参数化，逐条独立报告，哪条挂了直接看名字）；
- `python tests/xxx.py`（保留原有直接运行习惯）。

不引入 pytest 之外的任何新依赖。
"""
from __future__ import annotations

import traceback
from typing import Any, Callable, Sequence

PassCase = tuple[str, Callable[[], Any]]
RejectCase = tuple[str, type, Callable[[], Any]]
FailMsgCase = tuple[str, Callable[[], Any], str]


def _tail(exc: BaseException) -> str:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__, limit=2)
    return lines[-1].strip() if lines else f"{type(exc).__name__}: {exc}"


def run_cli(
    title: str,
    cases: Sequence[PassCase] = (),
    rejects: Sequence[RejectCase] = (),
    failmsgs: Sequence[FailMsgCase] = (),
) -> int:
    """命令行直跑模式，返回进程退出码（0=全绿）。"""
    results: list[tuple[str, bool, str]] = []

    for name, fn in cases:
        try:
            ok = bool(fn())
            detail = "assertion held" if ok else "assertion failed"
        except Exception as e:  # noqa: BLE001 — 测试跑板必须吃掉异常继续跑完
            ok, detail = False, _tail(e)
        results.append((name, ok, detail))

    for name, exc_type, fn in rejects:
        try:
            fn()
        except exc_type:
            ok, detail = True, f"correctly raised {exc_type.__name__}"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"raised unexpected {type(e).__name__}: {e}"
        else:
            ok, detail = False, "NO exception raised (rejection missing)"
        results.append((name, ok, detail))

    for name, fn, needle in failmsgs:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            ok = needle in msg
            results.append((name, ok, f"blocked: {needle}" if ok else f"missing needle: {msg[:160]}"))
        else:
            results.append((name, False, "NO exception raised (rejection missing)"))

    print("=" * 60)
    print(title)
    print("=" * 60)
    failed = 0
    for case, ok, detail in results:
        if not ok:
            failed += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {case}  — {detail}")
    print("-" * 60)
    total = len(results)
    print(f"{total - failed}/{total} PASS")
    return 1 if failed else 0


def pytest_cases(
    cases: Sequence[PassCase] = (),
    rejects: Sequence[RejectCase] = (),
    failmsgs: Sequence[FailMsgCase] = (),
):
    """生成 pytest 参数化测试函数，返回 (test_pass, test_reject, test_failmsg)。

    用法（在各测试文件末尾）：
        test_pass, test_reject, test_failmsg = casekit.pytest_cases(
            CASES, REJECT_CASES, FAILMSG_CASES)
    没有的那一类返回 None（赋给模块变量不会被 pytest 收集）。
    """
    import pytest

    def test_pass(case_name: str, case_fn: Callable[[], Any]) -> None:
        assert case_fn(), f"{case_name}：断言未成立"

    def test_reject(case_name: str, exc_type: type, case_fn: Callable[[], Any]) -> None:
        with pytest.raises(exc_type):
            case_fn()

    def test_failmsg(case_name: str, case_fn: Callable[[], Any], needle: str) -> None:
        with pytest.raises(Exception) as ei:
            case_fn()
        assert needle in str(ei.value), f"{case_name}：错误信息未点名 {needle!r}"

    out = []
    for table, fn, names in (
        (cases, test_pass, ("case_name", "case_fn")),
        (rejects, test_reject, ("case_name", "exc_type", "case_fn")),
        (failmsgs, test_failmsg, ("case_name", "case_fn", "needle")),
    ):
        if not table:
            out.append(None)
            continue
        rows = [(n, *rest) for n, *rest in table]
        out.append(
            pytest.mark.parametrize(
                ",".join(names), rows, ids=[r[0] for r in rows]
            )(fn)
        )
    return tuple(out)


__all__ = ["run_cli", "pytest_cases"]
