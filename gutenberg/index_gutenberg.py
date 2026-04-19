import re, time, io, zipfile, gzip, csv, urllib.request, sys, ssl, os
import pymysql

LANGUAGES = os.environ.get("GUTENBERG_LANGUAGES", "en").split(',')
CHUNK_CHARS = 600
BATCH_SIZE  = 300
SLEEP_SEC   = 1.0

MANTICORE_HOST = '127.0.0.1'
MANTICORE_PORT = 9306

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
BOOKS_BASE_DIR = os.path.join(SCRIPT_DIR, "books")
BOOKS_TXT_DIR  = os.environ.get("GUTENBERG_TXT_DIR", os.path.join(BOOKS_BASE_DIR, "txt"))
BOOKS_INDEX_DIR = os.path.join(BOOKS_BASE_DIR, "index")

# Catalog URL — single ~14 MB gzipped CSV, much more complete than the harvest robot
CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(s):
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:80]

def get_conn():
    return pymysql.connect(
        host=MANTICORE_HOST, port=MANTICORE_PORT,
        user='', password='', database='',
        charset='utf8mb4', connect_timeout=10,
    )

def create_table(conn):
    cur = conn.cursor()
    # Remove the DROP — let it persist across runs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gutenberg_paragraphs (
            book_id   integer,
            title     text,
            author    text,
            language  string,
            start_char integer,
            body      text
        ) morphology='stem_en,libstemmer_fr,libstemmer_de,libstemmer_it,libstemmer_es'
          min_stemming_len='2'
          index_sp='1'
    """)
    conn.commit()
    print("Table ready.")

def bulk_insert(conn, rows):
    if not rows:
        return
    cur = conn.cursor()
    sql = ("INSERT INTO gutenberg_paragraphs "
           "(book_id,title,author,language,start_char,body) "
           "VALUES (%s,%s,%s,%s,%s,%s)")
    cur.executemany(sql, rows)
    conn.commit()

# ---------------------------------------------------------------------------
# Catalog download
# ---------------------------------------------------------------------------

def fetch_catalog(wanted_langs):
    os.makedirs(BOOKS_BASE_DIR, exist_ok=True)
    catalog_path = os.path.join(BOOKS_BASE_DIR, "pg_catalog.csv")

    if os.path.exists(catalog_path):
        print(f"Using cached catalog: {catalog_path}")
        with open(catalog_path, 'rb') as f:
            csv_bytes = f.read()
    else:
        print(f"Downloading catalog from {CATALOG_URL} ...")
        hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
        ctx  = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE

        req = urllib.request.Request(CATALOG_URL, headers=hdrs)
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            raw_gz = r.read()

        print(f"  Downloaded {len(raw_gz):,} bytes. Decompressing...")
        csv_bytes = gzip.decompress(raw_gz)

        with open(catalog_path, 'wb') as f:
            f.write(csv_bytes)

        print(f"  Catalog saved to {catalog_path}")

    # Parse CSV
    reader = csv.DictReader(io.StringIO(csv_bytes.decode('utf-8', errors='replace')))
    rows = list(reader)

    def norm(d):
        return {k.strip().lstrip('\ufeff'): v.strip() for k, v in d.items()}
    rows = [norm(r) for r in rows]

    wanted = set(l.strip().lower() for l in wanted_langs)
    kept = []
    for r in rows:
        if r.get('Type', '').lower() != 'text':
            continue
        lang = r.get('Language', '').strip().lower()
        if lang not in wanted:
            continue
        kept.append(r)

    print(f"  {len(rows):,} total entries → {len(kept):,} text books in {wanted_langs}")
    return kept

# ---------------------------------------------------------------------------
# Per-book download
# ---------------------------------------------------------------------------

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx

def build_zip_urls(book_id):
    sid = str(book_id)
    urls =[]

    # For newer books (ID >= 100), the cache endpoint usually works
    if book_id >= 100:
        urls.append(f"https://www.gutenberg.org/cache/epub/{sid}/pg{sid}.txt.utf8")

    # Primary modern plain-text URLs
    urls.append(f"https://www.gutenberg.org/files/{sid}/{sid}-0.txt") # UTF-8
    urls.append(f"https://www.gutenberg.org/files/{sid}/{sid}-8.txt") # ISO-8859-1 (Common for older FR/DE texts)
    urls.append(f"https://www.gutenberg.org/files/{sid}/{sid}.txt")   # ASCII fallback

    # Aleph mirror zips (last resort)
    if len(sid) == 1:
        dir_path = f"0/{sid}"
    else:
        dir_path = "/".join(sid[:-1]) + f"/{sid}"
        
    urls.append(f"https://aleph.gutenberg.org/{dir_path}/{sid}-0.zip")
    urls.append(f"https://aleph.gutenberg.org/{dir_path}/{sid}-8.zip")
    urls.append(f"https://aleph.gutenberg.org/{dir_path}/{sid}.zip")

    return urls

def _decode_raw_text(data, hint=None):
    """Detect encoding from Gutenberg header and decode safely."""
    header = data[:2000].decode("ascii", errors="ignore")
    enc_match = re.search(r'Character set encoding:\s*([a-zA-Z0-9\-]+)', header, re.IGNORECASE)
    
    if enc_match:
        encoding = enc_match.group(1).lower()
    elif hint:
        encoding = hint
    else:
        encoding = "utf-8"
        
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        # Fallback sequence: UTF-8 -> ISO-8859-1 -> Replace
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return data.decode("iso-8859-1")
            except:
                return data.decode("utf-8", errors="replace")

def _decode_zip(data):
    """Extract and decode the first .txt entry from a zip blob."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith(".txt"):
                raw_bytes = z.read(name)
                # Apply encoding hint based on filename conventions
                hint = "iso-8859-1" if name.endswith("-8.txt") else "utf-8"
                return _decode_raw_text(raw_bytes, hint)
    return None

def download_text(book_id):
    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    ctx  = _ssl_ctx()

    for url in build_zip_urls(book_id):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                data = r.read()
                
            if url.endswith(".zip"):
                result = _decode_zip(data)
                if result:
                    return result
            else:
                hint = "iso-8859-1" if url.endswith("-8.txt") else "utf-8"
                return _decode_raw_text(data, hint)
                
        except Exception:
            continue  # try next URL silently

    print(f"    WARNING: all URLs failed for book {book_id}")
    return None

# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------

def chunk_prose(text):
    paras    = re.split(r'\n\s*\n', text)
    chunks   =[]
    buf      = ""
    buf_start = 0
    offset   = 0
    for para in paras:
        para = para.strip()
        if not para:
            offset += 2
            continue
        if len(buf) + len(para) > CHUNK_CHARS and buf:
            chunks.append((buf.strip(), buf_start))
            buf_start = offset
            buf = ""
        buf    += " " + para
        offset += len(para) + 2
    if buf.strip():
        chunks.append((buf.strip(), buf_start))
    return chunks

def is_already_indexed(conn, book_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM gutenberg_paragraphs WHERE book_id = %s LIMIT 1",
        (book_id,)
    )
    return cur.fetchone()[0] > 0

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\nConnecting to Manticore {MANTICORE_HOST}:{MANTICORE_PORT}...")
    try:
        conn = get_conn()
    except Exception as e:
        print(f"ERROR: Cannot connect: {e}")
        print("Make sure the ManticoreSearch service is running.")
        raise

    create_table(conn)
    os.makedirs(BOOKS_TXT_DIR, exist_ok=True)

    catalog = fetch_catalog(LANGUAGES)

    from collections import defaultdict
    per_lang = defaultdict(list)
    for row in catalog:
        per_lang[row['Language'].strip().lower()].append(row)

    print("")
    print("  Books available per language:")
    for lang, rows in sorted(per_lang.items()):
        print(f"    {lang}: {len(rows):,}")
    total_available = sum(len(v) for v in per_lang.values())
    print(f"    ─────────────────")
    print(f"    Total: {total_available:,}")
    print("")

    # Automatically skip limits if you pass CLI arguments, otherwise prompt
    if len(sys.argv) > 1:
        limit_input = sys.argv[1]
    else:
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
    print("")

    if max_books > 0:
        catalog =[]
        for lang_rows in per_lang.values():
            catalog.extend(lang_rows[:max_books])

    total = 0
    batch =[]

    for idx, row in enumerate(catalog, 1):
        book_id = int(row.get('Text#', 0) or 0)
        title   = row.get('Title',   'Unknown').strip() or 'Unknown'
        author  = row.get('Authors', 'Unknown').strip() or 'Unknown'
        lang    = row.get('Language', '').strip().lower()

        if not book_id:
            continue

        book_filename = (
            f"{sanitize_filename(author)} - {sanitize_filename(title)} ({lang}).txt"
        )
        book_path = os.path.join(BOOKS_TXT_DIR, book_filename)

        print(f"[{idx}/{len(catalog)}] #{book_id} — {title[:60]}")

        if os.path.exists(book_path) and is_already_indexed(conn, book_id):
            print(f"    Already indexed, skipping.")
            continue
        elif os.path.exists(book_path):
            print(f"    File exists, indexing from disk.")
            with open(book_path, encoding='utf-8', errors='replace') as f:
                raw = f.read()
        else:
            raw = download_text(book_id)
            if not raw:
                continue
            with open(book_path, 'w', encoding='utf-8') as f:
                f.write(raw)
            print(f"    Saved: books/txt/{book_filename}")
            time.sleep(SLEEP_SEC)

        if len(raw) < 200:
            print(f"    SKIPPED: text too short ({len(raw)} chars).")
            continue

        chunks = chunk_prose(raw)
        if not chunks:
            print(f"    SKIPPED: no chunks generated.")
            continue

        for chunk_text, start_char in chunks:
            batch.append((book_id, title, author, lang, start_char, chunk_text))
            if len(batch) >= BATCH_SIZE:
                bulk_insert(conn, batch)
                total += len(batch)
                print(f"    {total} paragraphs indexed...")
                batch = []

    if batch:
        bulk_insert(conn, batch)
        total += len(batch)

    conn.close()
    print(f"\nDone! {total} total paragraphs indexed.")

if __name__ == "__main__":
    main()
