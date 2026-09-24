@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  Mail Workbench V2 · 工作桶界面
rem
rem  双击本文件即可。它会：
rem    1. 复用 V1 的 launch.py，保证 watchdog.py + server.py 在跑
rem       （V2 是内置在 server.py 进程里的，不需要单独启动一个服务）
rem    2. 打开浏览器到 http://127.0.0.1:8080/workbench
rem
rem  与 V1 的区别：
rem    /              V1 经典三栏视图（文件夹 / 列表 / 阅读窗格）—— 保持不变
rem    /workbench     V2 工作桶视图（待我处理 / 等对方 / 草稿就绪 / 已延后 / 今天完成）
rem                   支持快捷键、批处理、AI 草稿审核、发送前人工确认
rem
rem  V2 后台能力（随 server.py 一起启动，无需额外操作）：
rem    - IMAP IDLE 事件驱动同步（不再靠 automation 轮询）
rem    - 本地 scheduler：snooze / follow-up / 队列自愈 / 早报预计算
rem    - DraftJobQueue（SQLite）+ WorkBuddy Worker 契约
rem ============================================================

set "PYW="
for /d %%v in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do (
  if not defined PYW if exist "%%~v\pythonw.exe" set "PYW=%%~v\pythonw.exe"
  if not defined PYW if exist "%%~v\python.exe" set "PYW=%%~v\python.exe"
)
for %%i in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:i"
for %%i in (python.exe) do if not defined PYW set "PYW=%%~$PATH:i"

if not defined PYW (
  echo.
  echo   找不到 Python 解释器。请先双击本目录下的 install.cmd。
  echo.
  pause
  exit /b 1
)

rem 先确保服务在跑（launch.py 是幂等的：已在跑就直接退出）
"%PYW%" "%~dp0launch.py"
if errorlevel 1 (
  echo.
  echo   [警告] 启动器返回了错误码，说明它中途出错了。
  echo           详情见 _launch.log（里面会有完整的异常堆栈）。
  echo.
)

rem 等服务端口就绪（最多 40 秒 —— 首次连接 IMAP 有时偏慢，20 秒不够）
set /a tries=0
:wait
set /a tries+=1
powershell -NoProfile -Command "try{ (New-Object Net.Sockets.TcpClient).Connect('127.0.0.1',8080); exit 0 }catch{ exit 1 }" >nul 2>&1
if %errorlevel%==0 goto ready
if %tries% lss 40 (
  timeout /t 1 /nobreak >nul
  goto wait
)

rem 走到这里说明服务确实没起来。原来这里直接打开浏览器就 exit 了 ——
rem 窗口一闪而过，用户只看到「页面显示失败」，看不到任何原因。
rem 所以：给出排查顺序，并且 pause 住，别让窗口跑掉。
echo.
echo   [警告] 等了 40 秒，服务仍未就绪。
echo.
echo   排查顺序（从最可能的开始）：
echo     1. _launch.log    启动器自己的记录（含异常堆栈）
echo     2. _watchdog.log  守护进程有没有在拉服务
echo     3. _server.log    服务自身的输出与崩溃堆栈
echo.
echo   如果这三个文件都没新内容，多半是 Python 解释器不对 —— 先双击 install.cmd。
echo.
pause

:ready
start "" "http://127.0.0.1:8080/workbench"
exit
