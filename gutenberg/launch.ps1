$host.UI.RawUI.WindowTitle = "Project Gutenberg - Running"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir    = Join-Path $ScriptDir "..\venv"
$ManticoreDir = Join-Path $ScriptDir "manticore"
$ManticoreBin = Join-Path $ManticoreDir "bin\searchd.exe"
$ManticoreConf = Join-Path $ManticoreDir "manticore.conf"

try {

    # ============================================================
    # PHASE 0 - VALIDATE ENVIRONMENT
    # ============================================================
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

    # ============================================================
    # PHASE 0b - CHECK MCP SERVER IS RUNNING
    # ============================================================
    # The port can vary (--port flag), so we detect it from the process's open TCP connections.
    $getProcCmd = if (Get-Command Get-CimInstance -ErrorAction SilentlyContinue) { 'Get-CimInstance' } else { 'Get-WmiObject' }
    $mcpProcess = & $getProcCmd Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match "mcp_server\.py" } |
        Select-Object -First 1

    if (-not $mcpProcess) {
        Write-Host ""
        Write-Host "============================================================" -ForegroundColor Red
        Write-Host "  ERROR: MCP SERVER IS NOT RUNNING" -ForegroundColor Red
        Write-Host "============================================================" -ForegroundColor Red
        Write-Host ""
        Write-Host "  mcp_server.py does not appear to be running."
        Write-Host "  Please run 'Local-MCP-server\launch.bat' and keep that window open."
        Write-Host ""
        Read-Host "  Press Enter to exit..."
        exit 1
    }

    # Resolve which port the MCP server is listening on
    $mcpPort = $null
    $tcpConns = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -eq $mcpProcess.ProcessId }
    if ($tcpConns) {
        $mcpPort = ($tcpConns | Select-Object -First 1).LocalPort
    }

    Write-Host ""
    Write-Host "============================================================"
    Write-Host " PROJECT GUTENBERG - RUNNING"
    Write-Host "============================================================"
    Write-Host ""

    if ($mcpPort) {
        Write-Host " [OK] MCP server detected on port $mcpPort (PID $($mcpProcess.ProcessId))"
    } else {
        Write-Host " [OK] MCP server detected (PID $($mcpProcess.ProcessId), port unknown)"
    }
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

} catch {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host "  UNEXPECTED ERROR" -ForegroundColor Red
    Write-Host "============================================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "  $_" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "  Press Enter to exit..."
    exit 1
}
