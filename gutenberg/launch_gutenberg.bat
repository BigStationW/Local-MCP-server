@echo off
setlocal
title Project Gutenberg - Running

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENV_DIR=%SCRIPT_DIR%\..\venv"

if not exist "%VENV_DIR%" (
    echo.
    echo ============================================================
    echo   ERROR: PYTHON VIRTUAL ENVIRONMENT NOT FOUND
    echo ============================================================
    echo.
    echo   The 'venv' directory is missing.
    echo   Please run 'Local-MCP-server\launch.bat' first to create that folder.
    echo.
    pause
    exit /b 1
)

"%VENV_DIR%\Scripts\python.exe" "%SCRIPT_DIR%\index_gutenberg.py"

endlocal