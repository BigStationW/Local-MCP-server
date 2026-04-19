$host.UI.RawUI.WindowTitle = "Gutenberg Prose Search - Running"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManticoreDir = Join-Path $ScriptDir "manticore"
$ManticoreBin = Join-Path $ManticoreDir "bin\searchd.exe"
$ManticoreConf = Join-Path $ManticoreDir "manticore.conf"

Write-Host ""
Write-Host "============================================================"
Write-Host " GUTENBERG PROSE SEARCH - LAUNCH"
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