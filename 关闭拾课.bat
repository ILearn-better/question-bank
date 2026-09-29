@echo off
chcp 65001 >nul
title 拾课 · 停止服务
cd /d "%~dp0"

REM ============================================================
REM  拾课 · 双击停止服务
REM
REM  为什么不能按「所有 python」杀：会顺手杀掉你其它的 python 程序。
REM  实测出来的两种进程形态（都得能收掉）：
REM    · 普通模式：命令行里有 uvicorn，可执行文件在项目 .venv 下 —— 好认
REM    · 热重载（dev）模式的**子进程**：这两个特征一个都没有（命令行是
REM      multiprocessing.spawn、可执行文件被解析成基础解释器），只认得出
REM      「它占着我们的端口」。所以判据是两条，不是一条。
REM  另外父进程用 taskkill /T 连子树一起杀 —— 否则子进程会变成
REM  「端口还占着但没人管」的孤儿（实测踩过）。
REM
REM  换端口启动过的话，这里也要给同样的端口：set SHIKE_PORT=8020
REM ============================================================

set "PORT=8000"
if not "%SHIKE_PORT%"=="" set "PORT=%SHIKE_PORT%"

echo.
echo   正在停止拾课【端口 %PORT%】……
echo.

REM 中文提示一律留在 bat 里，PowerShell 只输出 ASCII —— 免得两套编码混在一起出现乱码。
powershell -NoProfile -ExecutionPolicy Bypass -Command "$port=%PORT%; $proj=(Get-Location).Path.TrimEnd('\').ToLower(); $k=@(); $all=@(Get-CimInstance Win32_Process); foreach($p in $all){ $c='' + $p.CommandLine; if($c.ToLower().Contains('uvicorn') -and ((($p.ExecutablePath) -and $p.ExecutablePath.ToLower().StartsWith($proj)) -or $c.ToLower().Contains($proj))){ & taskkill /PID $p.ProcessId /T /F 2>&1 | Out-Null; $k+=$p.ProcessId } }; Start-Sleep -Milliseconds 900; foreach($c in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)){ $o=$all | Where-Object { $_.ProcessId -eq $c.OwningProcess }; $nm='unknown'; if($o){ $nm='' + $o.Name }; if($o -and $o.Name -like 'python*'){ & taskkill /PID $c.OwningProcess /T /F 2>&1 | Out-Null; $k+=$c.OwningProcess } else { Write-Host ('port %PORT% held by ' + $nm + ' (PID ' + $c.OwningProcess + ') - not python, left alone') } }; Start-Sleep -Milliseconds 600; $left=@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count; if($k.Count -gt 0){ Write-Host ('stopped PID ' + ($k -join ', ')) } else { Write-Host 'no shike service found' }; Write-Host ('port ' + $port + ' listeners left: ' + $left)"

echo.
echo   stopped PID ...    = 已经停掉了（那几个是服务的进程号）
echo   no shike service   = 本来就没在跑
echo   not ours, left     = 端口被别的软件占着，脚本没动它
echo.
echo   数据都在本机，随时双击「启动拾课.bat」继续用。
echo.
pause
