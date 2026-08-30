@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  start.bat  - 韩语小说批量翻译工具启动脚本
rem  逻辑: 优先使用 .venv -> 否则用系统 Python 建 .venv
rem        -> 仍无 Python 则下载到当前目录并建 .venv -> 装依赖
rem ============================================================

set "APP_DIR=%~dp0"
set "VENV_PYTHON=%APP_DIR%.venv\Scripts\python.exe"
set "MAIN=%APP_DIR%main.py"
set "REQ=%APP_DIR%requirements.txt"
set "SETUP_DIR=%APP_DIR%._setup"
set "LOCAL_PYTHON=%APP_DIR%.python\python.exe"
set "PY_VERSION=3.12.10"
set "PY_INSTALLER=%SETUP_DIR%\python-%PY_VERSION%-amd64.exe"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
set "DEPS_OK=%SETUP_DIR%\deps_ok"

if not exist "%MAIN%" (
    echo [ERROR] 未找到主程序: %MAIN%
    echo 请确认 start.bat 位于项目根目录后再运行。
    pause
    exit /b 1
)

rem ----- 已有虚拟环境则直接进入依赖检查 -----
if exist "%VENV_PYTHON%" goto :ready

rem ===================== 需要创建虚拟环境 =====================
set "SYS_PYTHON="
where python >nul 2>nul && set "SYS_PYTHON=python"
if defined SYS_PYTHON goto :create_env
where py >nul 2>nul && set "SYS_PYTHON=py"
if defined SYS_PYTHON goto :create_env

rem ----- 系统中没有可用 Python，自动下载安装 -----
echo [INFO] 未检测到系统 Python，开始自动下载安装 Python %PY_VERSION% ...
call :download_python
if errorlevel 1 (
    echo [ERROR] Python 自动安装失败，请检查网络或手动安装 Python 后重试。
    pause
    exit /b 1
)
echo [INFO] 正在使用本地 Python 创建虚拟环境 ...
"%LOCAL_PYTHON%" -m venv "%APP_DIR%.venv"
if not exist "%VENV_PYTHON%" (
    echo [ERROR] 创建虚拟环境失败: %VENV_PYTHON%
    echo 可尝试删除 .venv 目录后重新运行本脚本。
    pause
    exit /b 1
)
goto :ready

:create_env
echo [INFO] 正在使用系统 Python (%SYS_PYTHON%) 创建虚拟环境 ...
%SYS_PYTHON% -m venv "%APP_DIR%.venv"
if not exist "%VENV_PYTHON%" (
    echo [WARN] 系统 Python 无法创建虚拟环境，尝试自动下载安装 ...
    call :download_python
    if errorlevel 1 (
        echo [ERROR] Python 自动安装失败，请检查网络或手动安装 Python 后重试。
        pause
        exit /b 1
    )
    echo [INFO] 正在使用本地 Python 创建虚拟环境 ...
    "%LOCAL_PYTHON%" -m venv "%APP_DIR%.venv"
)
if not exist "%VENV_PYTHON%" (
    echo [ERROR] 创建虚拟环境失败: %VENV_PYTHON%
    echo 可尝试删除 .venv 目录后重新运行本脚本。
    pause
    exit /b 1
)
goto :ready

:ready
if exist "%DEPS_OK%" (
    echo [OK] 依赖已安装，跳过依赖安装。
    goto :launch
)
echo [INFO] 正在安装 requirements.txt 依赖（已安装的会自动跳过）...
"%VENV_PYTHON%" -m pip install -r "%REQ%"
if errorlevel 1 (
    echo [ERROR] 依赖安装失败，请检查网络后重试。
    echo 提示: 可手动运行 "%VENV_PYTHON%" -m pip install -r "%REQ%"
    pause
    exit /b 1
)
if not exist "%SETUP_DIR%" mkdir "%SETUP_DIR%" >nul 2>nul
echo ok > "%DEPS_OK%"

:launch
echo [OK] 环境就绪，正在启动 main.py ...
"%VENV_PYTHON%" "%MAIN%" %*

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo 程序已结束，退出码: %EXIT_CODE%
pause
exit /b %EXIT_CODE%

rem ===================== 子过程：下载并本地安装 Python =====================
:download_python
if not exist "%SETUP_DIR%" mkdir "%SETUP_DIR%" >nul 2>nul
if not exist "%PY_INSTALLER%" (
    echo [INFO] 正在下载 Python %PY_VERSION% 安装包，请稍候 ...
    curl.exe -L -sS -o "%PY_INSTALLER%" "%PY_URL%" 2>nul
    if not exist "%PY_INSTALLER%" (
        echo [INFO] curl 不可用，改用系统下载组件 ...
        certutil -urlcache -split -f "%PY_URL%" "%PY_INSTALLER%" >nul 2>nul
    )
    if not exist "%PY_INSTALLER%" (
        echo [ERROR] 下载 Python 安装包失败，请检查网络连接后重试。
        exit /b 1
    )
)
echo [INFO] 正在安装 Python 到当前目录，不会影响系统环境 ...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=0 TargetDir="%APP_DIR%.python" Include_launcher=0 Include_test=0 Include_doc=0 Include_pip=1 Include_tcltk=1 Include_dev=0 Include_lib=1 Include_ensurepip=1
if errorlevel 1 (
    echo [ERROR] Python 安装失败，请尝试手动安装 Python 后再运行本脚本。
    exit /b 1
)
if not exist "%LOCAL_PYTHON%" (
    echo [ERROR] 未找到本地 Python 解释器: %LOCAL_PYTHON%
    exit /b 1
)
exit /b 0
