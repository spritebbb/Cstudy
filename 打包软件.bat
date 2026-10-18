@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [错误] 找不到虚拟环境 .venv
    echo 请先按 README.md 里的步骤建立环境。
    echo.
    pause
    exit /b 1
)

echo 开始打包成免安装文件夹（几分钟，别关窗口）……
echo.
".venv\Scripts\python.exe" scripts\build_package.py %*
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，上面有报错信息。
    echo.
    pause
    exit /b 1
)

echo.
echo 好了。成品在 dist\C语言备考助手\ 里。
pause
