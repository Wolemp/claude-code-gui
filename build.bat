@echo off
chcp 65001 >nul 2>&1
title Claude Code GUI - Build .exe
cd /d "%~dp0"

echo.
echo   ========================================
echo     Building Claude Code GUI .exe
echo   ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python is required to build .exe
    echo   Install: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Install build tools
echo   Installing PyInstaller...
pip install pyinstaller pywebview anthropic --quiet

:: Build
echo   Building standalone .exe (this may take a minute)...
echo.
pyinstaller --onefile --windowed --name ClaudeCodeGUI --clean --noconfirm main.py

if exist "dist\ClaudeCodeGUI.exe" (
    echo.
    echo   ========================================
    echo     Build successful!
    echo   ========================================
    echo.
    echo   .exe location: dist\ClaudeCodeGUI.exe
    echo   Size:
    for %%A in (dist\ClaudeCodeGUI.exe) do echo   %%~zA bytes
    echo.
    echo   This .exe runs on any Windows PC.
    echo   No Python, no install needed.
    echo   Upload to GitHub Releases for distribution.
    echo.
) else (
    echo.
    echo   [ERROR] Build failed. Check errors above.
)

:: Cleanup build artifacts
if exist "build" rmdir /s /q build 2>nul
if exist "ClaudeCodeGUI.spec" del ClaudeCodeGUI.spec 2>nul

pause
