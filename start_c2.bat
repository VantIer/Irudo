@echo off
REM Start C2 side (CLI mode by default; pass "web" for Web mode)
setlocal
set MODE=%1
if "%MODE%"=="" set MODE=cli
if not "%MODE%"=="cli" if not "%MODE%"=="web" (
    echo Usage: start_c2.bat [cli^|web]
    exit /b 1
)
python -m src.main --mode %MODE% --config config_c2.json
endlocal