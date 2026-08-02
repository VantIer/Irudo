@echo off
echo ========================================
echo Building Irudo Remote Agent Executable
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

REM Build the headless remote agent (no web / LLM / third-party deps)
echo Building...
pyinstaller --onefile --name irudo_remote ^
    --console ^
    remote/main.py

if %errorlevel% equ 0 (
    echo.
    echo ========================================
    echo Build completed successfully!
    echo Executable: dist\irudo_remote.exe
    echo ========================================
    move dist\irudo_remote.exe build\ >nul 2>&1
    echo Executable moved to: build\irudo_remote.exe
    echo.
    echo Usage:
    echo   build\irudo_remote.exe --c2-address 192.168.1.100:8881 --agent-id BOT-01 --auth-token token-for-server-01
    echo   build\irudo_remote.exe --config config_remote.json
    echo.
    echo NOTE: pass --config with an absolute path to your config_remote.json
    echo       (a config is NOT embedded into the executable).
) else (
    echo.
    echo Build failed!
)

echo.
pause
