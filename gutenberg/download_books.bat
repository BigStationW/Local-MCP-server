@echo off
setlocal
title Project Gutenberg - Download ^& Index

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENV_DIR=%SCRIPT_DIR%\..\venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "INDEX_SCRIPT=%SCRIPT_DIR%\index_gutenberg.py"

:: ============================================================
:: PHASE 0 - VALIDATE ENVIRONMENT
:: ============================================================
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

if not exist "%PYTHON_EXE%" (
    echo.
    echo   ERROR: Python not found in venv.
    echo.
    pause
    exit /b 1
)

if not exist "%INDEX_SCRIPT%" (
    echo.
    echo   ERROR: index_gutenberg.py not found.
    echo.
    pause
    exit /b 1
)

:: ============================================================
:: HAND OFF EVERYTHING ELSE TO PYTHON
:: ============================================================
"%PYTHON_EXE%" "%INDEX_SCRIPT%" --setup

endlocal