$host.UI.RawUI.WindowTitle = "Gutenberg Prose Search - Download & Index"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir    = Join-Path $ScriptDir "..\venv"

$BooksDir       = Join-Path $ScriptDir "books"
$BooksTxtDir    = Join-Path $BooksDir  "txt"
$BooksIndexDir  = Join-Path $BooksDir  "index"

$ManticoreDir  = Join-Path $ScriptDir "manticore"
$ManticoreBin  = Join-Path $ManticoreDir "bin\searchd.exe"
$ManticoreConf = Join-Path $ManticoreDir "manticore.conf"
$ManticoreData = $BooksIndexDir
$ManticoreLogs = Join-Path $ManticoreDir "logs"
$ManticoreZip  = Join-Path $ScriptDir "manticore_pkg.zip"

$IndexScript = Join-Path $ScriptDir "index_gutenberg.py"
$PythonExe   = Join-Path $VenvDir "Scripts\python.exe"

Write-Host ""
Write-Host "============================================================"
Write-Host "  GUTENBERG PROSE SEARCH - DOWNLOAD & INDEX  (no Docker)"
Write-Host "============================================================"
Write-Host ""
Write-Host "This will:"
Write-Host "  1. Ask which languages to download"
Write-Host "  2. Download and extract Manticore Search (zip, no installer)"
Write-Host "  3. Start it as a background process (no service needed)"
Write-Host "  4. Download the Gutenberg corpus (plain text only) -> books\txt"
Write-Host "  5. Index all books into Manticore (index files)    -> books\index"
Write-Host ""
Read-Host "Press Enter to continue"

# ============================================================
# STEP 1 - LANGUAGE SELECTION
# ============================================================

Write-Host ""
Write-Host "[1/5] Language selection"
Write-Host ""
Write-Host "  Enter the languages you want to download from Gutenberg."
Write-Host "  Use 2-letter codes separated by commas (example: en, la)."
Write-Host ""
Write-Host "  Common codes:"
Write-Host "    en = English    fr = French    de = German"
Write-Host "    it = Italian    es = Spanish   pt = Portuguese"
Write-Host "    nl = Dutch      fi = Finnish   la = Latin"
Write-Host ""

$LanguagesInput = Read-Host "Your languages (default: en)"
if ([string]::IsNullOrWhiteSpace($LanguagesInput)) { $LanguagesInput = "en" }

$Languages = $LanguagesInput.Split(',') |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -ne "" }

Write-Host ""
Write-Host "  Will download: $($Languages -join ', ')"
Write-Host ""

# ============================================================
# STEP 2 - DOWNLOAD AND EXTRACT MANTICORE
# ============================================================

Write-Host "[2/5] Setting up Manticore Search..."
Write-Host ""

foreach ($dir in @($ManticoreDir, $ManticoreData, $ManticoreLogs,
                   $BooksDir, $BooksTxtDir, $BooksIndexDir)) {
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
        Write-Host "  Please download manually from:"
        Write-Host "  https://manticoresearch.com/install/"
        Write-Host "  Extract into: $ManticoreDir"
        Read-Host "Press Enter to exit"
        exit 1
    }

    Write-Host "  Extracting..."
    try {
        Expand-Archive -Path $ManticoreZip -DestinationPath $ManticoreDir -Force
        Remove-Item $ManticoreZip -Force
    }
    catch {
        Write-Host "  ERROR: Extraction failed: $_"
        Read-Host "Press Enter to exit"
        exit 1
    }

    if (-not (Test-Path $ManticoreBin)) {
        Write-Host "  ERROR: searchd.exe not found after extraction."
        Get-ChildItem $ManticoreDir -Recurse -Filter "searchd.exe"
        Read-Host "Press Enter to exit"
        exit 1
    }

    Write-Host "  Manticore extracted successfully."
}

# Write config
Write-Host ""
Write-Host "  Writing Manticore config..."

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

Write-Host "  Config written."
Write-Host "  Index files : $ManticoreData"
Write-Host "  Book txt    : $BooksTxtDir"

# Kill any leftover searchd from a previous run
$stale = Get-Process -Name "searchd" -ErrorAction SilentlyContinue
if ($stale) {
    Write-Host "  Stopping leftover searchd process..."
    $stale | Stop-Process -Force
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Host "  Starting Manticore as a background process..."

$searchd = Start-Process `
    -FilePath $ManticoreBin `
    -ArgumentList "--config `"$ManticoreConf`"" `
    -PassThru `
    -WindowStyle Hidden


# ============================================================
# NEW WATCHER PROCESS LOGIC
# ============================================================
# Start a completely hidden watcher process that waits for THIS script to die, 
# and automatically cleans up searchd.exe if the "X" button is clicked.
$WatcherArgs = "-NoProfile -Command `"Wait-Process -Id $PID -ErrorAction SilentlyContinue; Stop-Process -Id $($searchd.Id) -Force -ErrorAction SilentlyContinue`""
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "powershell.exe"
$psi.Arguments = $WatcherArgs
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.CreateNoWindow = $true
$psi.UseShellExecute = $false
[System.Diagnostics.Process]::Start($psi) | Out-Null

# We still register the graceful exit event, but correctly pass variables using -MessageData
Register-EngineEvent -SourceIdentifier PowerShell.Exiting -MessageData $searchd.Id -Action {
    Get-Process -Id $Event.MessageData -ErrorAction SilentlyContinue | Stop-Process -Force
} | Out-Null
# ============================================================


# Poll port 9306 up to 30 seconds
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
    Write-Host "  ERROR: Manticore did not start within 30 seconds."
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[OK] ManticoreSearch ready on port 9306."

# ============================================================
# STEP 3 - PYTHON DEPS
# ============================================================

Write-Host ""
Write-Host "[3/5] Installing Python dependencies..."

if (-not (Test-Path $PythonExe)) {
    Write-Host "  ERROR: venv not found at $VenvDir"
    Read-Host "Press Enter to exit"
    exit 1
}

& $PythonExe -m pip install pymysql --quiet
Write-Host "  pymysql installed."

# ============================================================
# STEP 4 - PREPARE INDEX SCRIPT
# ============================================================

Write-Host ""
Write-Host "[4/5] Preparing index script..."

if (-not (Test-Path $IndexScript)) {
    Write-Host "  ERROR: index_gutenberg.py not found."
    Read-Host "Press Enter to exit"
    exit 1
}

$LangArray = ($Languages | ForEach-Object { "'$_'" }) -join ", "

$Content = Get-Content $IndexScript -Raw
$Content = [regex]::Replace(
    $Content,
    '(?m)^\s*LANGUAGES\s*=.*$',
    "LANGUAGES = [$LangArray]"
)
$Content | Set-Content $IndexScript -Encoding UTF8

Write-Host "  Index script ready with languages: $($Languages -join ', ')"

# ============================================================
# STEP 5 - RUN INDEXER
# ============================================================

Write-Host ""
Write-Host "[5/5] Starting indexer"
Write-Host ""

$env:GUTENBERG_TXT_DIR = $BooksTxtDir

# We only run this once because the Python script natively prompts for limits.
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
}
else {
    # Fallback in case the variable was lost
    Get-Process -Name "searchd" -ErrorAction SilentlyContinue | Stop-Process -Force
}

Write-Host "  [OK] Stopped."
Write-Host ""
Write-Host "  Run launch.ps1 to start Manticore Search."
Write-Host ""
Read-Host "Press Enter to exit"
