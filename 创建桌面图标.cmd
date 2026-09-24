@echo off
chcp 65001 >nul
rem ============================================================
rem  邮件工作台 · 建桌面图标
rem
rem  双击本文件，桌面上就会多一个「邮件工作台」图标。
rem  以后双击那个图标 = 起服务 + 打开浏览器，不用记目录。
rem
rem  安装向导（install.cmd）最后一步会自动做这件事，
rem  本文件是给「当时选了跳过、后来又想要」的人用的。
rem ============================================================

set "MW=%~dp0"

rem 拿真实桌面路径 —— 开了 OneDrive「桌面备份」的机器会被重定向，
rem 直接写 %USERPROFILE%\Desktop 可能建到没人看的地方。
set "DESK=%USERPROFILE%\Desktop"
for /f "delims=" %%i in ('powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')" 2^>nul') do (
  if not "%%i"=="" set "DESK=%%i"
)

if not exist "%DESK%" (
  echo.
  echo   找不到桌面目录：%DESK%
  echo.
  pause
  exit /b 1
)

> "%DESK%\邮件工作台.cmd" echo @echo off
>> "%DESK%\邮件工作台.cmd" echo rem 邮件工作台 —— 双击即可（起服务 + 打开浏览器）
>> "%DESK%\邮件工作台.cmd" echo start "" /min "%MW%启动邮件工作台.cmd"

echo.
echo   桌面图标已建好：
echo.
echo     %DESK%\邮件工作台.cmd
echo.
echo   以后双击它就行了。删掉它也不影响程序本体。
echo.
pause
