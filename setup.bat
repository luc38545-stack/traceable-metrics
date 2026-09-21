@echo off
chcp 65001 >nul
cd /d "%~dp0"
title TraceableMetrics 环境安装

echo.
echo   TraceableMetrics 环境安装
echo   将在项目目录创建独立 .venv，不修改系统 Python 依赖。
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
  echo [安装失败] 未找到 Python 3。请从 https://www.python.org/downloads/ 安装 Python 3.12+，并勾选 Add Python to PATH。
  pause
  exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [1/3] 创建项目虚拟环境...
  %PY% %PY_ARGS% -m venv "%~dp0.venv"
  if errorlevel 1 goto :failed
  set "PY=%~dp0.venv\Scripts\python.exe"
  set "PY_ARGS="
)

echo [2/3] 安装运行依赖（锁定版本 requirements.lock.txt）...
%PY% -m pip install --upgrade pip
if errorlevel 1 goto :failed
%PY% -m pip install -r requirements.lock.txt
if errorlevel 1 goto :failed

echo [3/3] 安装测试依赖（锁定版本 requirements-dev.lock.txt）...
if exist "%~dp0requirements-dev.lock.txt" %PY% -m pip install -r requirements-dev.lock.txt
if errorlevel 1 goto :failed

echo.
echo 安装完成。现在可以双击 启动工作台.bat，或运行 ci_check.bat。
pause
exit /b 0

:failed
echo.
echo [安装失败] 请检查网络、Python 版本和 pip 错误信息后重试。
pause
exit /b 1
