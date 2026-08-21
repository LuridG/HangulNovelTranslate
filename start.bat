@echo off
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "MAIN=%~dp0main.py"

if not exist "%PYTHON%" (
    echo [ERROR] 未找到虚拟环境: %PYTHON%
    echo 请先运行: python -m venv .venv
    pause
    exit /b 1
)

if not exist "%MAIN%" (
    echo [ERROR] 未找到主程序: %MAIN%
    pause
    exit /b 1
)

echo 正在通过虚拟环境启动 main.py ...
"%PYTHON%" "%MAIN%" %*

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo 程序已结束，退出码: %EXIT_CODE%
pause
exit /b %EXIT_CODE%