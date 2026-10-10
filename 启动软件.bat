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

".venv\Scripts\pythonw.exe" run.py
if errorlevel 1 (
    echo.
    echo [错误] 启动失败，详细信息在 logs\cstudy.log
    echo.
    pause
)
