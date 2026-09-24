@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  邮件工作台 · 前台模式（用来排查问题）
rem
rem  和「启动V2工作台.cmd」的区别：
rem    那个是无窗口后台跑 + 自动开浏览器；
rem    这个是**前台跑**，日志直接打在窗口里，关掉窗口 = 停掉服务。
rem
rem  平时用不到，只在「服务起不来、想看看报什么错」时用。
rem ============================================================

set "PY="

rem 1) PATH 里有没有 python
for %%i in (python.exe) do if not defined PY set "PY=%%~$PATH:i"

rem 2) WorkBuddy 内置解释器
for /d %%v in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if not defined PY if exist "%%~v\python.exe" set "PY=%%~v\python.exe"
)

if not defined PY (
  echo.
  echo   找不到 Python 解释器。
  echo   请先双击本目录下的 install.cmd 完成安装。
  echo.
  pause
  exit /b 1
)

echo 邮件工作台 · 前台模式
echo 地址：http://127.0.0.1:8080    (按 Ctrl+C 停止)
echo ------------------------------------------------------------
"%PY%" server.py --port 8080
pause
