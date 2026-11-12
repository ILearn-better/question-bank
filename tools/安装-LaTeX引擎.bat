@echo off
chcp 936 >nul
setlocal enabledelayedexpansion

rem 装 LaTeX 引擎（tectonic）。单文件 50MB，所以不进仓库，按需下载。
rem 装好之后「导出 PDF」才会用真 LaTeX 排版；没装也能用，只是回退到旧的贴图路径。

set "HERE=%~dp0"
set "VENDOR=%HERE%..\backend\vendor"
set "EXE=%VENDOR%\tectonic.exe"

if exist "%EXE%" (
  echo 已经装好了：%EXE%
  "%EXE%" --version
  goto :done
)

if not exist "%VENDOR%" mkdir "%VENDOR%"

rem GitHub 直连在本机不通（502），走加速镜像。失效了就把这行换回 github.com 直链。
set "URL=https://ghproxy.net/https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%%400.15.0/tectonic-0.15.0-x86_64-pc-windows-msvc.zip"
set "ZIP=%TEMP%\tectonic-0.15.0.zip"

echo 正在下载 LaTeX 引擎（约 19MB）……
curl -L --fail -o "%ZIP%" "%URL%"
if errorlevel 1 goto :fail

echo 正在解压……
tar -xf "%ZIP%" -C "%VENDOR%"
if errorlevel 1 goto :fail
del "%ZIP%" >nul 2>&1

if not exist "%EXE%" goto :fail

echo.
echo 装好了：%EXE%
"%EXE%" --version
echo.
echo 提示：第一次导出 PDF 需要联网下载排版宏包（约 5 分钟，只此一次），
echo       之后每次几秒。宏包缓存在 %LOCALAPPDATA%\TectonicProject。
goto :done

:fail
echo.
echo 下载或解压失败。可以手动下载后解压到 backend\vendor\：
echo   %URL%
echo 或用浏览器打开 https://github.com/tectonic-typesetting/tectonic/releases
echo 选 tectonic-0.15.0-x86_64-pc-windows-msvc.zip

:done
echo.
pause
