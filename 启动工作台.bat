@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title TraceableMetrics 本地工作台

echo.
echo   ========================================================
echo    TraceableMetrics 本地工作台
echo    浏览器地址 http://127.0.0.1:8765
echo    关闭本窗口即停止服务
echo   ========================================================
echo.

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
  echo [启动失败] 未找到 Python 3。请先双击 setup.bat 完成安装。
  pause
  exit /b 1
)

%PY% %PY_ARGS% -c "import duckdb,yaml,dbt" >nul 2>nul
if errorlevel 1 (
  echo [启动失败] 运行依赖未安装。请先双击 setup.bat。
  pause
  exit /b 1
)

%PY% %PY_ARGS% -u "%~dp0serve_cli.py"

echo.
echo   服务已停止。按任意键关闭窗口...
pause >nul
