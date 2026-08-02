@echo off
echo ========================================
echo Building Irudo C2 Executable
echo ========================================
echo.

REM Get script directory and go to project root
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%.."

REM Create build directory if not exists
if not exist "build" mkdir build

REM Install pyinstaller if not installed
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing pyinstaller...
    pip install pyinstaller
)

REM Build the C2 executable (CLI + Web modes share this binary)
echo Building...
pyinstaller --onefile --name irudo_c2 ^
    --add-data "web;web" ^
    --hidden-import uvicorn ^
    --hidden-import fastapi ^
    --hidden-import openai ^
    --hidden-import httpx ^
    --collect-all uvicorn ^
    --collect-all fastapi ^
    --collect-all openai ^
    --console ^
    src/main.py

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo Build completed successfully!
    echo Executable: dist\irudo_c2.exe
    echo ========================================
    move dist\irudo_c2.exe build\ >nul 2>&1
    echo Executable moved to: build\irudo_c2.exe
    echo.
    echo Usage:
    echo   build\irudo_c2.exe --mode cli --config config_c2.json
    echo   build\irudo_c2.exe --mode web --config config_c2.json
    echo.
    echo NOTE: pass --config with an absolute path to your config_c2.json
    echo       (a config is NOT embedded into the executable).
) else (
    echo.
    echo Build failed!
)

echo.
pause
