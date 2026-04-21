import re, time, io, zipfile, gzip, csv, urllib.request, sys, ssl, os
import subprocess
import socket
import shutil
import argparse
import pymysql
import urllib.parse

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

# Gutendex — free, no-auth REST API over Project Gutenberg's catalog
GUTENDEX_BASE = "https://gutendex.com/books"

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
# MANTICORE SETUP
# ============================================================

def setup_manticore(verbose=True):
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

    for d in [MANTICORE_DIR, BOOKS_BASE_DIR, MANTICORE_LOGS]:
        os.makedirs(d, exist_ok=True)

    if os.path.exists(MANTICORE_BIN):
        if verbose:
            print("  Manticore binary already found, skipping download.")
    else:
        _download_manticore()

    _write_manticore_conf()
    _kill_searchd()

    searchd_proc = subprocess.Popen(
        [MANTICORE_BIN, '--config', MANTICORE_CONF],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    import atexit
    def _cleanup():
        try:
            if searchd_proc.poll() is None:
                searchd_proc.kill()
                searchd_proc.wait(timeout=5)
        except Exception:
            pass
    atexit.register(_cleanup)

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
# MCP CHECK
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

def sync_metadata_if_changed(conn, book_id, new_title, new_author, new_bookshelves):
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute(
        "SELECT title, author, bookshelves FROM gutenberg_paragraphs WHERE book_id = %s LIMIT 1",
        (book_id,)
    )
    result = cur.fetchone()

    if not result:
        return False

    current_title      = result['title']
    current_author     = result['author']
    current_bookshelves = result['bookshelves']

    if (current_title != new_title or
        current_author != new_author or
        current_bookshelves != new_bookshelves):

        print("    Metadata has changed. Updating...")
        update_cur = conn.cursor()
        update_cur.execute(
            """
            UPDATE gutenberg_paragraphs
            SET title=%s, author=%s, bookshelves=%s
            WHERE book_id=%s
            """,
            (new_title, new_author, new_bookshelves, book_id)
        )
        conn.commit()
        print("    [OK] Updated.")

    return True

# ============================================================
# GUTENDEX CATALOG  (replaces CSV bulk download)
# ============================================================

def _gutendex_request(url):
    """
    Make a single HTTPS GET to the Gutendex API and return parsed JSON.
    Retries once on transient errors.
    """
    import json
    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            raise RuntimeError(f"Gutendex request failed: {e}") from e

# --- NEW FUNCTION ---
def get_book_count_for_lang(lang):
    """Queries Gutendex just to get the total book count for a language."""
    params = urllib.parse.urlencode({
        "languages": lang,
        "mime_type": "text/plain",
    })
    url = f"{GUTENDEX_BASE}?{params}"
    try:
        data = _gutendex_request(url)
        return data.get("count", 0)
    except Exception as e:
        print(f"  [WARNING] Could not fetch book count for '{lang}': {e}")
        return 0

def iter_catalog(wanted_langs, max_per_lang=0):
    """
    Generator that yields one book-info dict at a time from the Gutendex API.
    """
    for lang in wanted_langs:
        lang = lang.strip().lower()
        params = urllib.parse.urlencode({
            "languages": lang,
            "mime_type": "text/plain",
            "sort": "ascending",
        })
        url = f"{GUTENDEX_BASE}?{params}"
        delivered = 0

        print(f"\n  Fetching catalog for language '{lang}' from Gutendex...")

        while url:
            data = _gutendex_request(url)
            results = data.get("results", [])

            for book in results:
                if max_per_lang and delivered >= max_per_lang:
                    break

                book_id = book.get("id")
                if not book_id:
                    continue

                title = book.get("title", "Unknown").strip() or "Unknown"

                authors = book.get("authors", [])
                author = "; ".join(a.get("name", "") for a in authors) or "Unknown"

                languages = book.get("languages", [])
                language  = languages[0] if languages else lang

                bookshelves_raw = book.get("bookshelves", [])
                bookshelves = "; ".join(
                    re.sub(r'Category:\s*', '', b, flags=re.IGNORECASE)
                    for b in bookshelves_raw
                )

                formats   = book.get("formats", {})
                text_url  = _pick_text_url(formats)

                yield {
                    "book_id":    book_id,
                    "title":      title,
                    "author":     author,
                    "language":   language,
                    "bookshelves": bookshelves,
                    "text_url":   text_url,
                }
                delivered += 1

            if max_per_lang and delivered >= max_per_lang:
                break
            url = data.get("next")

        print(f"  Done with '{lang}': {delivered} books processed.")


def _pick_text_url(formats):
    """
    Choose the best plain-text download URL from a Gutendex formats dict.
    """
    preference = [
        "text/plain; charset=utf-8",
        "text/plain; charset=us-ascii",
        "text/plain",
    ]
    for mime in preference:
        if mime in formats:
            return formats[mime]

    for mime, url in formats.items():
        if "text/plain" in mime:
            return url

    return None


def get_book_info(book_id):
    """Fetch metadata + format URLs for a single book by its Gutenberg ID."""
    return _gutendex_request(f"{GUTENDEX_BASE}/{book_id}")

# ============================================================
# DOWNLOAD
# ============================================================

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def _decode_raw_text(data, hint=None):
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

def _decode_zip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith(".txt"):
                raw_bytes = z.read(name)
                hint = "windows-1252" if name.endswith("-8.txt") else "utf-8"
                return _decode_raw_text(raw_bytes, hint)
    return None

def download_text(text_url, book_id=None):
    if not text_url:
        print(f"  [WARNING] No text URL for book {book_id}, skipping.")
        return None

    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    ctx = _ssl_ctx()
    urls_to_try = [text_url]
    if book_id:
        urls_to_try.append(None)

    for url in urls_to_try:
        if url is None:
            try:
                info    = get_book_info(book_id)
                formats = info.get("formats", {})
                fresh   = _pick_text_url(formats)
                if not fresh or fresh == text_url: continue
                url = fresh
                print(f"    -> Retrying with fresh URL from Gutendex: {url}")
            except Exception as e:
                print(f"    -> Could not fetch fresh URL: {e}")
                continue

        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                if r.status >= 400:
                    print(f"    -> FAILED with HTTP {r.status}: {url}")
                    continue
                data = r.read()
            print(f"    -> SUCCESS: {url}")
            return _decode_zip(data) if url.endswith(".zip") else _decode_raw_text(data, "windows-1252" if "-8.txt" in url else "utf-8")
        except Exception as e:
            print(f"    -> FAILED: {e} | URL: {url}")
            continue

    print(f"  [WARNING] All URLs failed for book {book_id}")
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

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true', help='Run full setup.')
    parser.add_argument('--serve', action='store_true', help='Start Manticore and keep it running.')
    args = parser.parse_args()

    searchd_proc = None

    if args.serve:
        print("\n  Checking MCP server...")
        pid, port = find_mcp_process()
        if pid is None:
            print("\n============================================================")
            print("  ERROR: MCP SERVER IS NOT RUNNING")
            print("============================================================")
            print("\n  Please run 'Local-MCP-server\\launch.bat' and keep that window open.\n")
            input("  Press Enter to exit...")
            sys.exit(1)
        print("  MCP server detected")
        searchd_proc = setup_manticore(verbose=False)
        print("============================================================")
        print("  MANTICORE SEARCH IS RUNNING")
        print("============================================================")
        print(f"\n  Listening on {MANTICORE_HOST}:{MANTICORE_PORT}")
        print("  Keep this window open while using the MCP tools.")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            print("\n  Shutting down...")
        if searchd_proc and searchd_proc.poll() is None:
            searchd_proc.kill()
            searchd_proc.wait(timeout=5)
        print("  [OK] Stopped.")
        sys.exit(0)

    elif args.setup:
        searchd_proc = setup_manticore()
    else:
        print("\n  Checking MCP server...")
        pid, port = find_mcp_process()
        if pid is None:
            print("\n============================================================")
            print("  ERROR: MCP SERVER IS NOT RUNNING")
            print("============================================================")
            print("\n  Please run 'Local-MCP-server\\launch.bat' and keep that window open.\n")
            input("  Press Enter to exit...")
            sys.exit(1)
        print("\n============================================================")
        print(" PROJECT GUTENBERG - RUNNING")
        print("============================================================")
        if port:
            print(f"\n  [OK] MCP server detected on port {port} (PID {pid})")
        else:
            print(f"\n  [OK] MCP server detected (PID {pid}, port unknown)")
        print()

    print(f"  Connecting to Manticore {MANTICORE_HOST}:{MANTICORE_PORT}...")
    try:
        conn = get_conn()
    except Exception as e:
        print(f"  ERROR: Cannot connect: {e}")
        print("  Make sure the ManticoreSearch service is running.")
        raise
    create_table(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT book_id) FROM gutenberg_paragraphs")
        row = cur.fetchone()
        books_in_db_total = row[0] if row else 0

    print("\n------------------------------------------------------------")
    print("  USER INPUT REQUIRED")
    print("------------------------------------------------------------\n")
    print("  Select the languages of the books you want to download.")
    print("  Use 2-letter codes separated by commas (example: en, fr).\n")
    print("  Common codes:")
    print("    en = English    fr = French    de = German")
    print("    it = Italian    es = Spanish   pt = Portuguese")
    print("    nl = Dutch      fi = Finnish   la = Latin\n")
    
    lang_input = input("  Type the languages here (default: en): ").strip() or "en"
    wanted_langs = [l.strip().lower() for l in lang_input.split(',') if l.strip()]

    # Store counts so we can display them in the progress tracker [current/total]
    lang_totals_map = {}
    print("\n  Checking available books...")
    for lang in wanted_langs:
        count = get_book_count_for_lang(lang)
        lang_totals_map[lang] = count
        print(f"  - For language '{lang}': {count:,} books found.")
    print()

    limit_input = input("  How many books per language? (Just press Enter for all): ").strip()
    if not limit_input or limit_input.lower() == "all":
        max_books = 0
        print("  FULL MODE: all available books.")
    elif limit_input.isdigit() and int(limit_input) > 0:
        max_books = int(limit_input)
        print(f"  LIMITED MODE: up to {max_books:,} per language.")
    else:
        max_books = 0
        print("  Unrecognized input, defaulting to full download.")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT book_id) FROM gutenberg_paragraphs")
        row = cur.fetchone()
        books_in_db_total = row[0] if row else 0

    print("\n------------------------------------------------------------")
    print("  INDEXER STARTING")
    print("------------------------------------------------------------")

    # Track how many we have processed in THIS session specifically per language
    lang_session_counters = {l: 0 for l in wanted_langs}
    total_paragraphs_indexed_this_session = 0

    for book_info in iter_catalog(wanted_langs, max_per_lang=max_books):
        book_id    = book_info["book_id"]
        title      = book_info["title"]
        author     = book_info["author"]
        language   = book_info["language"].lower()
        bookshelves = book_info["bookshelves"]
        text_url   = book_info["text_url"]

        # Increment counter for the specific language
        if language in lang_session_counters:
            lang_session_counters[language] += 1
        else:
            # Fallback for unexpected language codes
            lang_session_counters[language] = lang_session_counters.get(language, 0) + 1

        # Determine the denominator for the [X/Y] display
        # If user set a limit (max_books), use that. Otherwise use the API count.
        api_total = lang_totals_map.get(language, 0)
        display_total = max_books if (max_books > 0 and max_books < api_total) else api_total

        current_idx = lang_session_counters[language]
        print(f"[{current_idx}/{display_total}] #{book_id} — {title[:60]}")

        # Check if already indexed / update metadata
        if sync_metadata_if_changed(conn, book_id, title, author, bookshelves):
            print("    Already indexed, skipping.")
            continue

        if not text_url:
            print("    SKIPPED: no plain-text format.")
            continue

        raw = download_text(text_url, book_id=book_id)
        if not raw:
            continue

        time.sleep(SLEEP_SEC)
        chunks = chunk_prose(raw)
        if not chunks:
            continue

        # Prepare batch for this book
        book_batch = []
        for chunk_text, start_char in chunks:
            book_batch.append((book_id, title, author, language, bookshelves, start_char, chunk_text))
        
        if book_batch:
            bulk_insert(conn, book_batch)
            total_paragraphs_indexed_this_session += len(book_batch)
            books_in_db_total += 1 
            print(f"    [OK] Book #{book_id} indexed ({len(book_batch)} paragraphs).")
            print(f"    TOTAL BOOKS IN DATABASE: {books_in_db_total:,}")

    conn.close()
    
    print("\n============================================================")
    print("  INDEXING COMPLETE")
    print("============================================================")
    print(f"  New paragraphs added: {total_paragraphs_indexed_this_session:,}")
    print(f"  Total books now in DB: {books_in_db_total:,}\n")

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
