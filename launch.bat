@echo off
setlocal enabledelayedexpansion

:: Set the venv folder name
set VENV_DIR=venv
set FIRST_RUN=0

:: Check if Python is installed
echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Create virtual environment if it doesn't exist
if not exist "%VENV_DIR%" (
    echo Creating virtual environment...
    set FIRST_RUN=1
    python -m venv %VENV_DIR%
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
    echo.
)

:: Activate virtual environment
call %VENV_DIR%\Scripts\activate.bat >nul 2>&1
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)

:: Install packages based on whether it's first run
if "%FIRST_RUN%"=="1" (
    echo Upgrading pip...
    python -m pip install --upgrade pip
    
    echo Installing required packages...
    if exist requirements.txt (
        python -m pip install -r requirements.txt
        if errorlevel 1 (
            echo ERROR: Failed to install requirements.
            pause
            exit /b 1
        )
    )
    
    echo Installing Playwright browsers...
    python -m playwright install chromium --with-deps
) else (
    :: Subsequent runs - quiet updates
    python -m pip install --upgrade pip --quiet >nul 2>&1
    if exist requirements.txt (
        python -m pip install -r requirements.txt --quiet >nul 2>&1
    )
    python -m playwright install chromium >nul 2>&1
)

:: Run the MCP server
echo.
echo ================================
echo  MCP Server Starting...
echo ================================
echo.
python mcp_server.py --port 4241

:: Deactivate virtual environment when done
deactivate

pause