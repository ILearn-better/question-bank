@echo off
chcp 65001 >nul
title 拾课 · 本地服务
cd /d "%~dp0"

REM ============================================================
REM  拾课 · 双击启动（日常使用就双击这个文件）
REM
REM  为什么是 .bat 而不是 .ps1：PowerShell 脚本双击默认是「编辑」，
REM  还受执行策略拦，.bat 双击就是跑。
REM
REM  可选环境变量（一般用不到）：
REM    SHIKE_PORT=8020        换端口（默认 8000）
REM    SHIKE_NO_BROWSER=1     启动后不自动开浏览器
REM  想开热重载（改代码立即生效，日常使用不必开）：
REM    启动拾课.bat dev
REM ============================================================

set "PORT=8000"
if not "%SHIKE_PORT%"=="" set "PORT=%SHIKE_PORT%"

REM ---- 已在跑就别再起一个（重复双击很常见）：直接开页面 ----
REM 用 curl 探一下端口；连不上说明没在跑。curl.exe 是 Win10 1803+ 自带的。
curl -s -o nul -m 2 "http://127.0.0.1:%PORT%/" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo   拾课已经在运行了【端口 %PORT%】—— 直接打开页面。
    echo   要重启的话：先关掉那个服务窗口，再双击本文件。
    echo.
    if not "%SHIKE_NO_BROWSER%"=="1" start "" "http://127.0.0.1:%PORT%/"
    exit /b 0
)

REM ---- 虚拟环境 ----
if not exist "backend\.venv\Scripts\python.exe" (
    echo.
    echo   没找到虚拟环境 backend\.venv —— 先照 README 装一次依赖：
    echo       cd backend
    echo       python -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo   拾课正在启动……
echo   这个黑窗口就是服务本体，用完直接关掉它，就等于停止服务。
echo   地址： http://127.0.0.1:%PORT%/
echo   数据全部存在本机，运行时不联网。
echo.

REM ---- 过几秒再开浏览器：等它起来，免得先看到「无法访问」 ----
REM 用 ping 等 4 秒而不是 timeout：timeout 在没有交互输入的窗口里会直接报错退出。
REM 这里刻意不给 start 传空标题（""），否则外层 cmd /c "..." 的引号会配错。
if not "%SHIKE_NO_BROWSER%"=="1" (
    start "" /min cmd /c "ping -n 5 127.0.0.1 >nul & start http://127.0.0.1:%PORT%/"
)

cd backend
set "RELOAD="
if /i "%~1"=="dev" (
    set "RELOAD=--reload"
    echo   【开发模式】已开热重载：改后端代码会自动重启。
    echo   注意：这样每保存一次文件就真重启一次，而重启会跑数据库迁移。
    echo   要改数据结构时，先用临时目录起第二个服务，别在真库上试。
    echo.
)

".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port %PORT% %RELOAD%

echo.
echo   服务已停止【端口 %PORT%】。
echo   如果上面有红色报错，把这一屏截图发给 Copilot。
pause
