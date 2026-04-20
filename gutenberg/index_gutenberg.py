import re, time, io, zipfile, gzip, csv, urllib.request, sys, ssl, os
import subprocess
import socket
import shutil
import argparse
import pymysql

CHUNK_CHARS = 600
BATCH_SIZE = 300
SLEEP_SEC = 1.0

MANTICORE_HOST = '127.0.0.1'
MANTICORE_PORT = 9306

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS_BASE_DIR = os.path.join(SCRIPT_DIR, "books")

MANTICORE_DIR  = os.path.join(SCRIPT_DIR, "manticore")
MANTICORE_BIN  = os.path.join(MANTICORE_DIR, "bin", "searchd.exe")
MANTICORE_CONF = os.path.join(MANTICORE_DIR, "manticore.conf")
MANTICORE_LOGS = os.path.join(MANTICORE_DIR, "logs")
MANTICORE_ZIP  = os.path.join(SCRIPT_DIR, "manticore_pkg.zip")
MANTICORE_URL  = (
    "https://repo.manticoresearch.com/repository/manticoresearch_windows"
    "/release/x64/manticore-25.0.0-26032712-ce3c27828-x64-bundle.zip"
)

CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"

UNICODE_REPLACEMENTS = [
    ('\u2019', "'"), ('\u2018', "'"), ('\u02bc', "'"),
    ('\u0060', "'"), ('\u00b4', "'"), ('\u201c', '"'),
    ('\u201d', '"'), ('\u201e', '"'), ('\u2014', ' -- '),
    ('\u2013', '-'), ('\u2011', '-'), ('\u00ad', ''),
    ('\u200b', ''), ('\ufeff', ''),
]

# ============================================================
# UNICODE
# ============================================================

def normalize_unicode_punctuation(text):
    for src, dst in UNICODE_REPLACEMENTS:
        text = text.replace(src, dst)
    return text

# ============================================================
# MANTICORE SETUP  (replaces download_books.ps1 logic)
# ============================================================

def setup_manticore(verbose=True):
    """
    Full setup flow that was previously in the .ps1:
      - Create directories
      - Download and extract Manticore if missing
      - Write manticore.conf
      - Kill any stale searchd
      - Start searchd in background
      - Wait until port 9306 is ready
      - Register cleanup so searchd dies when this process exits
    Returns the searchd Popen object.
    """
    if verbose:
        print()
        print("============================================================")
        print("  PROJECT GUTENBERG - DOWNLOAD & INDEX")
        print("============================================================")
        print()
        print("  Please wait while the environment is prepared...")
        print()
        print("  Setting up Manticore Search...")
        print()

    # --- Directories ---
    for d in [MANTICORE_DIR, BOOKS_BASE_DIR, MANTICORE_LOGS]:
        os.makedirs(d, exist_ok=True)

    # --- Download Manticore if needed ---
    if os.path.exists(MANTICORE_BIN):
        if verbose:
            print("  Manticore binary already found, skipping download.")
    else:
        _download_manticore()

    # --- Write config ---
    _write_manticore_conf()

    # --- Kill stale searchd ---
    _kill_searchd()

    # --- Start searchd in background ---
    searchd_proc = subprocess.Popen(
        [MANTICORE_BIN, '--config', MANTICORE_CONF],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Register cleanup: kill searchd when Python exits
    import atexit
    def _cleanup():
        try:
            if searchd_proc.poll() is None:
                searchd_proc.kill()
                searchd_proc.wait(timeout=5)
        except Exception:
            pass
    atexit.register(_cleanup)

    # --- Wait for port 9306 ---
    if verbose:
        print("  Waiting for Manticore to be ready...")
    ready = False
    for _ in range(30):
        time.sleep(1)
        try:
            with socket.create_connection(('127.0.0.1', MANTICORE_PORT), timeout=1):
                ready = True
                break
        except OSError:
            pass

    if not ready:
        print("  ERROR: Manticore did not start within 30 seconds.")
        input("  Press Enter to exit...")
        sys.exit(1)

    if verbose:
        print()
        print("  [OK] Environment ready.")
        print()

    return searchd_proc


def _download_manticore():
    print("  Downloading Manticore Search (version 25.0.0)...")
    print("  This is about 50-60MB, please wait...")

    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(MANTICORE_URL, headers=hdrs)
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            data = r.read()
        with open(MANTICORE_ZIP, 'wb') as f:
            f.write(data)
    except Exception as e:
        print(f"  ERROR: Download failed: {e}")
        input("  Press Enter to exit...")
        sys.exit(1)

    print("  Extracting...")
    try:
        with zipfile.ZipFile(MANTICORE_ZIP) as z:
            z.extractall(MANTICORE_DIR)
    except Exception as e:
        print(f"  ERROR: Extraction failed: {e}")
        input("  Press Enter to exit...")
        sys.exit(1)
    finally:
        if os.path.exists(MANTICORE_ZIP):
            os.remove(MANTICORE_ZIP)

    if not os.path.exists(MANTICORE_BIN):
        print("  ERROR: searchd.exe not found after extraction.")
        input("  Press Enter to exit...")
        sys.exit(1)


def _write_manticore_conf():
    data_fwd  = BOOKS_BASE_DIR.replace('\\', '/')
    logs_fwd  = MANTICORE_LOGS.replace('\\', '/')
    conf = (
        "searchd {\n"
        "    listen           = 127.0.0.1:9306:mysql\n"
        f"    log              = {logs_fwd}/searchd.log\n"
        f"    query_log        = {logs_fwd}/query.log\n"
        f"    pid_file         = {data_fwd}/searchd.pid\n"
        f"    data_dir         = {data_fwd}\n"
        "    query_log_format = sphinxql\n"
        "}\n"
    )
    with open(MANTICORE_CONF, 'w', encoding='utf-8') as f:
        f.write(conf)


def _kill_searchd():
    try:
        out = subprocess.check_output(
            ['tasklist', '/FI', 'IMAGENAME eq searchd.exe'],
            stderr=subprocess.DEVNULL, text=True
        )
        if 'searchd.exe' in out:
            print("  Stopping existing searchd process...")
            subprocess.call(
                ['taskkill', '/F', '/IM', 'searchd.exe'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(2)
    except Exception as e:
        print(f"  WARNING: Could not stop existing searchd: {e}")

# ============================================================
# MCP CHECK  (carried over from previous refactor)
# ============================================================

def find_mcp_process():
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where',
             "name='python.exe' or name='pythonw.exe'",
             'get', 'ProcessId,CommandLine', '/format:csv'],
            stderr=subprocess.DEVNULL, text=True
        )
    except Exception as e:
        print(f"  WARNING: Could not query processes via wmic: {e}")
        return None, None

    for line in out.splitlines():
        if 'mcp_server.py' in line:
            parts = line.split(',')
            pid_str = parts[-1].strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                port = find_mcp_port(pid)
                return pid, port

    return None, None


def find_mcp_port(pid):
    try:
        out = subprocess.check_output(
            ['netstat', '-ano'],
            stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return None

    for pattern in [
        rf'TCP\s+127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+{pid}',
        rf'TCP\s+0\.0\.0\.0:(\d+)\s+\S+\s+LISTENING\s+{pid}',
    ]:
        m = re.search(pattern, out)
        if m:
            return m.group(1)

    return None

# ============================================================
# DB HELPERS
# ============================================================

def get_conn():
    return pymysql.connect(
        host=MANTICORE_HOST, port=MANTICORE_PORT,
        user='', password='', database='',
        charset='utf8mb4', connect_timeout=10,
    )

def create_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gutenberg_paragraphs (
            book_id integer,
            title text,
            author text,
            language string,
            bookshelves string,
            start_char integer,
            body text
        ) morphology='stem_en,libstemmer_fr,libstemmer_de,libstemmer_it,libstemmer_es'
        min_stemming_len='2'
        index_sp='1'
    """)
    conn.commit()
    print("  Table ready.")

def bulk_insert(conn, rows):
    if not rows:
        return
    cur = conn.cursor()
    sql = ("INSERT INTO gutenberg_paragraphs "
           "(book_id,title,author,language,bookshelves,start_char,body) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s)")
    cur.executemany(sql, rows)
    conn.commit()

# ============================================================
# CATALOG
# ============================================================

def load_catalog_bytes():
    os.makedirs(BOOKS_BASE_DIR, exist_ok=True)
    catalog_path = os.path.join(BOOKS_BASE_DIR, "pg_catalog.csv")

    if os.path.exists(catalog_path):
        print(f"  Using cached catalog: {catalog_path}")
        with open(catalog_path, 'rb') as f:
            return f.read()

    print(f"  Downloading catalog from {CATALOG_URL} ...")
    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(CATALOG_URL, headers=hdrs)
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        raw_gz = r.read()

    print(f"  Downloaded {len(raw_gz):,} bytes. Decompressing...")
    csv_bytes = gzip.decompress(raw_gz)

    with open(catalog_path, 'wb') as f:
        f.write(csv_bytes)

    print(f"  Catalog saved to {catalog_path}")
    return csv_bytes


def parse_and_filter_catalog(csv_bytes, wanted_langs):
    reader = csv.DictReader(io.StringIO(csv_bytes.decode('utf-8', errors='replace')))
    rows = list(reader)

    def norm(d):
        return {k.strip().lstrip('\ufeff'): v.strip() for k, v in d.items()}

    rows = [norm(r) for r in rows]
    wanted = set(l.strip().lower() for l in wanted_langs)
    kept = [r for r in rows if r.get('Type', '').lower() == 'text'
            and r.get('Language', '').strip().lower() in wanted]

    print(f"  {len(rows):,} total entries → {len(kept):,} text books in {wanted_langs}")
    return kept

# ============================================================
# DOWNLOAD
# ============================================================

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def build_zip_urls(book_id):
    sid = str(book_id)
    urls = []

    if book_id >= 100:
        urls.append(f"https://www.gutenberg.org/cache/epub/{sid}/pg{sid}.txt.utf8")

    urls.append(f"https://www.gutenberg.org/files/{sid}/{sid}-0.txt")
    urls.append(f"https://www.gutenberg.org/files/{sid}/{sid}-8.txt")
    urls.append(f"https://www.gutenberg.org/files/{sid}/{sid}.txt")

    if len(sid) == 1:
        dir_path = f"0/{sid}"
    else:
        dir_path = "/".join(sid[:-1]) + f"/{sid}"

    urls.append(f"https://aleph.gutenberg.org/{dir_path}/{sid}-0.zip")
    urls.append(f"https://aleph.gutenberg.org/{dir_path}/{sid}-8.zip")
    urls.append(f"https://aleph.gutenberg.org/{dir_path}/{sid}.zip")

    return urls

def _decode_raw_text(data, hint=None, book_id=None):
    header = data[:2000].decode("ascii", errors="ignore")
    enc_match = re.search(r'Character set encoding:\s*([a-zA-Z0-9-]+)', header, re.IGNORECASE)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if enc_match:
            encoding = enc_match.group(1).lower()
        elif hint:
            encoding = hint
        else:
            encoding = "iso-8859-1"

        if encoding == "iso-8859-1":
            encoding = "windows-1252"

        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            text = data.decode("windows-1252", errors="replace")

    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return normalize_unicode_punctuation(text)

def _decode_zip(data, book_id=None):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith(".txt"):
                raw_bytes = z.read(name)
                hint = "windows-1252" if name.endswith("-8.txt") else "utf-8"
                return _decode_raw_text(raw_bytes, hint, book_id)
    return None

def download_text(book_id):
    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    ctx = _ssl_ctx()

    for url in build_zip_urls(book_id):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                data = r.read()

            if url.endswith(".zip"):
                result = _decode_zip(data, book_id)
                if result:
                    return result
            else:
                hint = "windows-1252" if url.endswith("-8.txt") else "utf-8"
                return _decode_raw_text(data, hint, book_id)

        except Exception:
            continue

    print(f"  WARNING: all URLs failed for book {book_id}")
    return None

# ============================================================
# TEXT PROCESSING
# ============================================================

def chunk_prose(text):
    parts = re.split(r'(\n\s*\n)', text)
    chunks = []
    current_chunk = ""
    chunk_start_offset = 0
    raw_offset = 0

    i = 0
    while i < len(parts):
        para_raw = parts[i]
        para_text = para_raw.strip()
        leading_ws = len(para_raw) - len(para_raw.lstrip())
        para_content_start = raw_offset + leading_ws

        if para_text:
            if not current_chunk:
                chunk_start_offset = para_content_start
            if len(current_chunk) + len(para_text) > CHUNK_CHARS and current_chunk:
                chunks.append((current_chunk, chunk_start_offset))
                current_chunk = ""
                chunk_start_offset = para_content_start
            current_chunk += ("\n\n" if current_chunk else "") + para_text

        raw_offset += len(para_raw)

        if i + 1 < len(parts):
            raw_offset += len(parts[i + 1])
            i += 2
        else:
            i += 1

    if current_chunk:
        chunks.append((current_chunk, chunk_start_offset))

    return chunks

def is_already_indexed(conn, book_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM gutenberg_paragraphs WHERE book_id = %s LIMIT 1",
        (book_id,)
    )
    return cur.fetchone()[0] > 0

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--setup', action='store_true',
        help='Run full setup (download Manticore, start it, then index). '
             'Used by download_books.bat.'
    )
    parser.add_argument(
        '--serve', action='store_true',
        help='Start Manticore and keep it running (no indexing). '
             'Used by launch_gutenberg.bat.'
    )
    args = parser.parse_args()

    searchd_proc = None

    if args.serve:
        # Check if MCP server is running FIRST
        print()
        print("  Checking MCP server...")
        pid, port = find_mcp_process()

        if pid is None:
            print()
            print("============================================================")
            print("  ERROR: MCP SERVER IS NOT RUNNING")
            print("============================================================")
            print()
            print("  Please run 'Local-MCP-server\\launch.bat' and keep that window open.")
            print()
            input("  Press Enter to exit...")
            sys.exit(1)

        print(f"  MCP server detected")

        # Only start Manticore if MCP server is running
        searchd_proc = setup_manticore(verbose=False)
        
        print("============================================================")
        print("  MANTICORE SEARCH IS RUNNING")
        print("============================================================")
        print()
        print(f"  Listening on {MANTICORE_HOST}:{MANTICORE_PORT}")
        print("  Keep this window open while using the MCP tools.")
        
        try:
            # Keep alive until interrupted
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  Shutting down...")
        
        if searchd_proc and searchd_proc.poll() is None:
            searchd_proc.kill()
            searchd_proc.wait(timeout=5)
        
        print("  [OK] Stopped.")
        sys.exit(0)

    elif args.setup:
        # Full ps1-equivalent flow
        searchd_proc = setup_manticore()
    else:
        # launch.bat flow: just check MCP server is running
        print()
        print("  Checking MCP server...")
        pid, port = find_mcp_process()

        if pid is None:
            print()
            print("============================================================")
            print("  ERROR: MCP SERVER IS NOT RUNNING")
            print("============================================================")
            print()
            print("  Please run 'Local-MCP-server\\launch.bat' and keep that window open.")
            print()
            input("  Press Enter to exit...")
            sys.exit(1)

        print()
        print("============================================================")
        print(" PROJECT GUTENBERG - RUNNING")
        print("============================================================")
        print()
        if port:
            print(f"  [OK] MCP server detected on port {port} (PID {pid})")
        else:
            print(f"  [OK] MCP server detected (PID {pid}, port unknown)")
        print()

    # Connect to Manticore
    print(f"  Connecting to Manticore {MANTICORE_HOST}:{MANTICORE_PORT}...")
    try:
        conn = get_conn()
    except Exception as e:
        print(f"  ERROR: Cannot connect: {e}")
        print("  Make sure the ManticoreSearch service is running.")
        raise

    create_table(conn)

    # Catalog
    csv_bytes = load_catalog_bytes()

    print("\n------------------------------------------------------------")
    print("  USER INPUT REQUIRED")
    print("------------------------------------------------------------\n")
    print("  Select the languages of the books you want to download from Project Gutenberg.")
    print("  Use 2-letter codes separated by commas (example: en, la).\n")
    print("  Common codes:")
    print("    en = English    fr = French    de = German")
    print("    it = Italian    es = Spanish   pt = Portuguese")
    print("    nl = Dutch      fi = Finnish   la = Latin\n")

    lang_input = input("  Type the languages here (default: en): ").strip()
    if not lang_input:
        lang_input = "en"

    wanted_langs = [l.strip().lower() for l in lang_input.split(',') if l.strip()]
    print(f"\n  Will download: {', '.join(wanted_langs)}\n")

    print("------------------------------------------------------------")
    print("  INDEXER SCRIPT STARTING")
    print("------------------------------------------------------------\n")

    catalog = parse_and_filter_catalog(csv_bytes, wanted_langs)

    from collections import defaultdict
    per_lang = defaultdict(list)
    for row in catalog:
        per_lang[row['Language'].strip().lower()].append(row)

    print("\n  Books available per language:")
    for lang, rows in sorted(per_lang.items()):
        print(f"  {lang}: {len(rows):,}")

    total_available = sum(len(v) for v in per_lang.values())
    print(f"  -----------------")
    print(f"  Total: {total_available:,}\n")

    limit_input = input("  How many books per language? (Just press Enter for all): ").strip()

    if not limit_input or limit_input.lower() == "all":
        max_books = 0
        print(f"  FULL MODE: all {total_available:,} books.")
    elif limit_input.isdigit() and int(limit_input) > 0:
        max_books = int(limit_input)
        capped = sum(min(len(v), max_books) for v in per_lang.values())
        print(f"  LIMITED MODE: up to {max_books:,} per language ({capped:,} total).")
    else:
        max_books = 0
        print(f"  Unrecognized input, defaulting to full download ({total_available:,} books).")

    print()

    if max_books > 0:
        catalog = []
        for lang_rows in per_lang.values():
            catalog.extend(lang_rows[:max_books])

    total = 0
    batch = []

    for idx, row in enumerate(catalog, 1):
        book_id = int(row.get('Text#', 0) or 0)
        title = row.get('Title', 'Unknown').strip() or 'Unknown'
        author = row.get('Authors', 'Unknown').strip() or 'Unknown'
        lang = row.get('Language', '').strip().lower()
        bookshelves = row.get('Bookshelves', '').strip()

        if not book_id:
            continue

        print(f"[{idx}/{len(catalog)}] #{book_id} — {title[:60]}")

        if is_already_indexed(conn, book_id):
            print("    Already indexed, skipping.")
            continue

        raw = download_text(book_id)
        if not raw:
            continue

        time.sleep(SLEEP_SEC)

        if len(raw) < 200:
            print(f"    SKIPPED: text too short ({len(raw)} chars).")
            continue

        chunks = chunk_prose(raw)
        if not chunks:
            print("    SKIPPED: no chunks generated.")
            continue

        for chunk_text, start_char in chunks:
            batch.append((book_id, title, author, lang, bookshelves, start_char, chunk_text))
            if len(batch) >= BATCH_SIZE:
                bulk_insert(conn, batch)
                total += len(batch)
                print(f"    {total} paragraphs indexed...")
                batch = []

    if batch:
        bulk_insert(conn, batch)
        total += len(batch)

    conn.close()

    print()
    print("============================================================")
    print("  INDEXING COMPLETE")
    print("============================================================")
    print()

    if searchd_proc is not None:
        print("  Stopping Manticore background process...")
        try:
            if searchd_proc.poll() is None:
                searchd_proc.kill()
                searchd_proc.wait(timeout=5)
            print("  [OK] Stopped.")
        except Exception as e:
            print(f"  WARNING: Could not stop searchd cleanly: {e}")
    
    print()
    input("  Press Enter to exit...")


if __name__ == "__main__":
    main()