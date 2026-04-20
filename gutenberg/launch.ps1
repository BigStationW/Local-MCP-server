$host.UI.RawUI.WindowTitle = "Project Gutenberg - Running"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir    = Join-Path $ScriptDir "..\venv"
$ManticoreDir = Join-Path $ScriptDir "manticore"
$ManticoreBin = Join-Path $ManticoreDir "bin\searchd.exe"
$ManticoreConf = Join-Path $ManticoreDir "manticore.conf"

# ============================================================
# PHASE 0 - VALIDATE ENVIRONMENT
# ============================================================
# Check if the venv directory exists. If not, instruct the user to run the main batch file.
if (-not (Test-Path $VenvDir)) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host "  ERROR: PYTHON VIRTUAL ENVIRONMENT NOT FOUND" -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "  The 'venv' directory is missing."
    Write-Host "  Please run 'Local-MCP-server\launch.bat' first to create that folder."
    Write-Host ""
    Read-Host "  Press Enter to exit..."
    exit
}

Write-Host ""
Write-Host "============================================================"
Write-Host " PROJECT GUTENBERG - RUNNING"
Write-Host "============================================================"
Write-Host ""

if (-not (Test-Path $ManticoreBin)) {
    Write-Host " ERROR: Manticore not found. Run download_books.ps1 first."
    Read-Host "Press Enter to exit"
    exit 1
}

if (-not (Test-Path $ManticoreConf)) {
    Write-Host " ERROR: manticore.conf not found. Run download_books.ps1 first."
    Read-Host "Press Enter to exit"
    exit 1
}

# Kill any existing searchd process before launching in console mode
$running = Get-Process -Name "searchd" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host " Stopping existing searchd process..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

Write-Host " [OK] Starting ManticoreSearch on 127.0.0.1:9306"
Write-Host " Press Ctrl+C to stop."
Write-Host ""

& $ManticoreBin --config $ManticoreConf --console
