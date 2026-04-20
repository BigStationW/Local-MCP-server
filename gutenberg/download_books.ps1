$host.UI.RawUI.WindowTitle = "Project Gutenberg - Download & Index"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir    = Join-Path $ScriptDir "..\venv"

$BooksDir       = Join-Path $ScriptDir "books"

$ManticoreDir  = Join-Path $ScriptDir "manticore"
$ManticoreBin  = Join-Path $ManticoreDir "bin\searchd.exe"
$ManticoreConf = Join-Path $ManticoreDir "manticore.conf"
$ManticoreData = $BooksDir

$ManticoreLogs = Join-Path $ManticoreDir "logs"
$ManticoreZip  = Join-Path $ScriptDir "manticore_pkg.zip"

$IndexScript = Join-Path $ScriptDir "index_gutenberg.py"
$PythonExe   = Join-Path $VenvDir "Scripts\python.exe"

Write-Host ""
Write-Host "============================================================"
Write-Host "  PROJECT GUTENBERG - DOWNLOAD & INDEX"
Write-Host "============================================================"
Write-Host ""
Write-Host "  Please wait while the environment is prepared..."
Write-Host ""

# ============================================================
# PHASE 1 - DOWNLOAD AND EXTRACT MANTICORE
# ============================================================

Write-Host "  Setting up Manticore Search..."
Write-Host ""

foreach ($dir in @($ManticoreDir, $ManticoreData, $ManticoreLogs, $BooksDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

if (Test-Path $ManticoreBin) {
    Write-Host "  Manticore binary already found, skipping download."
}
else {
    Write-Host "  Downloading Manticore Search (version 25.0.0)..."
    Write-Host "  This is about 50-60MB, please wait..."

    try {
        Invoke-WebRequest `
            -Uri "https://repo.manticoresearch.com/repository/manticoresearch_windows/release/x64/manticore-25.0.0-26032712-ce3c27828-x64-bundle.zip" `
            -OutFile $ManticoreZip `
            -UseBasicParsing
    }
    catch {
        Write-Host ""
        Write-Host "  ERROR: Download failed: $_"
        Read-Host "Press Enter to exit"
        exit 1
    }

    Write-Host "  Extracting..."
    Expand-Archive -Path $ManticoreZip -DestinationPath $ManticoreDir -Force
    Remove-Item $ManticoreZip -Force

    if (-not (Test-Path $ManticoreBin)) {
        Write-Host "  ERROR: searchd.exe not found after extraction."
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# Write config
$DataFwd = $ManticoreData.Replace('\', '/')
$LogsFwd = $ManticoreLogs.Replace('\', '/')

@"
searchd {
    listen           = 127.0.0.1:9306:mysql
    log              = $LogsFwd/searchd.log
    query_log        = $LogsFwd/query.log
    pid_file         = $DataFwd/searchd.pid
    data_dir         = $DataFwd
    query_log_format = sphinxql
}
"@ | Set-Content $ManticoreConf -Encoding UTF8

$stale = Get-Process -Name "searchd" -ErrorAction SilentlyContinue
if ($stale) {
    $stale | Stop-Process -Force
    Start-Sleep -Seconds 2
}

$searchd = Start-Process `
    -FilePath $ManticoreBin `
    -ArgumentList "--config `"$ManticoreConf`"" `
    -PassThru `
    -WindowStyle Hidden

# Watcher process
$WatcherArgs = "-NoProfile -Command `"Wait-Process -Id $PID -ErrorAction SilentlyContinue; Stop-Process -Id $($searchd.Id) -Force -ErrorAction SilentlyContinue`""
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "powershell.exe"
$psi.Arguments = $WatcherArgs
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow = $true
$psi.UseShellExecute = $false
[System.Diagnostics.Process]::Start($psi) | Out-Null

Register-EngineEvent -SourceIdentifier PowerShell.Exiting -MessageData $searchd.Id -Action {
    Get-Process -Id $Event.MessageData -ErrorAction SilentlyContinue | Stop-Process -Force
} | Out-Null

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $tcp = [System.Net.Sockets.TcpClient]::new("127.0.0.1", 9306)
        $tcp.Close()
        $ready = $true
        break
    } catch {}
}

if (-not $ready) {
    Write-Host "  ERROR: Manticore did not start."
    Read-Host "Press Enter to exit"
    exit 1
}

# ============================================================
# PHASE 2 - PYTHON DEPS
# ============================================================

if (-not (Test-Path $PythonExe)) {
    Write-Host "  ERROR: venv not found at $VenvDir"
    Read-Host "Press Enter to exit"
    exit 1
}

& $PythonExe -m pip install pymysql --quiet

# ============================================================
# PHASE 3 - RUN INDEXER
# ============================================================

if (-not (Test-Path $IndexScript)) {
    Write-Host "  ERROR: index_gutenberg.py not found."
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "  [OK] Environment ready."
Write-Host ""
Write-Host "------------------------------------------------------------"
Write-Host "  INDEXER SCRIPT STARTING"
Write-Host "------------------------------------------------------------"

# Call the Python script
& $PythonExe $IndexScript


# ============================================================
# DONE - STOP BACKGROUND PROCESS
# ============================================================

Write-Host ""
Write-Host "============================================================"
Write-Host "  INDEXING COMPLETE"
Write-Host "============================================================"
Write-Host ""
Write-Host "  Stopping Manticore background process..."

if ($searchd -and -not $searchd.HasExited) {
    $searchd.Kill()
    $searchd.WaitForExit(5000) | Out-Null
} else {
    Get-Process -Name "searchd" -ErrorAction SilentlyContinue | Stop-Process -Force
}

Write-Host "  [OK] Stopped."
Write-Host ""
Read-Host "Press Enter to exit"
