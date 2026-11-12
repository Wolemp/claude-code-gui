@echo off
setlocal enabledelayedexpansion
title Claude Code GUI
cd /d "%~dp0"

rem === Fast path: local Python already set up ===
if exist "python\python.exe" (
    if exist "python\Lib\site-packages\webview" (
        python\python.exe main.py
        exit /b
    )
    goto LOCAL_INSTALL
)

rem === Check system Python ===
python --version >nul 2>&1
if errorlevel 1 goto NO_PYTHON
echo   [OK] Python found
python --version
echo   [STEP] checking deps...
python -c "import webview, winpty, pyte" >nul 2>&1
if errorlevel 1 goto INSTALL_DEPS
echo   [OK] deps already installed, skipping pip
goto RUN_APP
:INSTALL_DEPS
echo   [STEP] installing missing deps (this may take a minute)...
pip install pywebview pywinpty pyte
pip install anthropic
:RUN_APP
echo   [STEP] launching main.py...
python -u main.py
echo   [EXIT] main.py exited with code %errorlevel%
pause
exit /b
:NO_PYTHON

rem === No Python - download portable version ===
echo.
echo   ========================================
echo     Claude Code GUI - Auto Setup
echo   ========================================
echo.
echo   Python not found on this system.
echo   Downloading portable Python...
echo.

set PYVER=3.12.4
set PYZIP=python-%PYVER%-embed-amd64.zip
set PYURL=https://www.python.org/ftp/python/%PYVER%/%PYZIP%

echo   Downloading Python %PYVER%...
powershell -Command "& {[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYZIP%' -UseBasicParsing}" 2>nul

if not exist "%PYZIP%" (
    curl -L -o "%PYZIP%" "%PYURL%" 2>nul
)

if not exist "%PYZIP%" (
    echo.
    echo   [ERROR] Download failed.
    echo   Please install Python manually:
    echo   https://www.python.org/downloads/
    pause
    exit /b 1
)

echo   [OK] Downloaded

echo   Extracting...
powershell -Command "& {Expand-Archive -Path '%PYZIP%' -DestinationPath 'python' -Force}" 2>nul
del "%PYZIP%" 2>nul

if not exist "python\python.exe" (
    echo   [ERROR] Extraction failed.
    pause
    exit /b 1
)
echo   [OK] Python extracted

echo   Configuring...
for %%f in (python\python*._pth) do (
    powershell -Command "(Get-Content '%%f') -replace '#import site','import site' | Set-Content '%%f'"
)

echo   Installing pip...
powershell -Command "& {[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'get-pip.py' -UseBasicParsing}" 2>nul
python\python.exe get-pip.py --quiet 2>nul
del get-pip.py 2>nul

:LOCAL_INSTALL
echo   Installing dependencies...
python\Scripts\pip.exe install pywebview anthropic pywinpty pyte --quiet 2>nul

if not exist "python\Lib\site-packages\webview" (
    echo.
    echo   [ERROR] Failed to install dependencies.
    echo   Try: python\Scripts\pip.exe install pywebview anthropic
    pause
    exit /b 1
)

echo.
echo   [OK] Setup complete!
echo.
echo   ========================================
echo     Starting Claude Code GUI...
echo   ========================================
echo.

python\python.exe main.py
