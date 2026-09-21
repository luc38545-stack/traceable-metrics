@echo off
chcp 65001 >nul
rem TraceableMetrics 一键自检：架构门禁 + 运行环境门禁 + pytest 全量
rem 需要双击前 webapp 未启动时，D12 浏览器测试会自动跳过。
cd /d "%~dp0"
set "PY_ARGS="
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PY=py"
    set "PY_ARGS=-3"
  )
)
if not defined PY (
  echo [检查失败] 未找到 Python 3。请先双击 setup.bat。
  pause
  exit /b 1
)
%PY% %PY_ARGS% ci_check.py
echo.
pause
