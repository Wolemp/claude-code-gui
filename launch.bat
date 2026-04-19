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
if not errorlevel 1 (
    echo   [OK] Python found
    pip install pywebview pywinpty pyte --quiet 2>nul
    pip install anthropic --quiet 2>nul
    python main.py
    exit /b
)

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
