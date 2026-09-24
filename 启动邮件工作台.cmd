@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  邮件工作台 · 启动 / 恢复
rem
rem  双击本文件即可。它会：
rem    1. 保证守护进程 watchdog.py 在跑（每 15 秒探活，服务挂了自己拉起来）
rem    2. 保证 server.py 在跑
rem    3. 打开浏览器到 http://127.0.0.1:8080
rem
rem  平时不用点 —— 登录 Windows 时会由启动文件夹自动执行。
rem  只有「面板打不开」时才需要双击一下。
rem
rem  Python 自动查找：先 WorkBuddy 内置解释器（版本号会变，用通配匹配），
rem  找不到再退回 PATH 里的 pythonw。都没找到就提示先跑 install.cmd。
rem ============================================================

set "PYW="

rem 1) 优先 WorkBuddy 内置 pythonw（不依赖 anaconda/系统 Python）
for /d %%v in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if not defined PYW if exist "%%~v\pythonw.exe" set "PYW=%%~v\pythonw.exe"
)

rem 2) 退回 PATH
for %%i in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:i"

if not defined PYW (
  echo.
  echo   找不到 Python 解释器。
  echo   请先双击本目录下的 install.cmd 完成安装。
  echo.
  pause
  exit /b 1
)

"%PYW%" "%~dp0launch.py"
exit
