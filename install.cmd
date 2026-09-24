@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 邮件工作台 · 安装向导

rem ============================================================
rem  双击本文件开始安装。
rem  它会自动找到 Python，然后启动 install.py 的向导。
rem ============================================================

set "PY="

rem 1) PATH 里的 python
for %%i in (python.exe) do if not defined PY set "PY=%%~$PATH:i"

rem 2) WorkBuddy 内置解释器
for /d %%v in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if not defined PY if exist "%%~v\python.exe" set "PY=%%~v\python.exe"
)

if not defined PY (
  echo.
  echo   找不到 Python。
  echo.
  echo   这台机器上需要一个 Python 3.8+ 才能装邮件工作台。
  echo   如果你装了 WorkBuddy，它内置的 Python 一般在：
  echo       %USERPROFILE%\.workbuddy\binaries\python\versions\
  echo.
  pause
  exit /b 1
)

"%PY%" install.py
pause
