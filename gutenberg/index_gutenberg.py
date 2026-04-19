import re, time, io, zipfile, urllib.request, sys, ssl, os
import pymysql
import itertools
LANGUAGES = ['en']
CHUNK_CHARS = 600
BATCH_SIZE  = 300
SLEEP_SEC   = 1.0
MAX_BOOKS   = int(sys.argv[1]) if len(sys.argv) > 1 else 0  # 0 = unlimited

MANTICORE_HOST = '127.0.0.1'
MANTICORE_PORT = 9306

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS_BASE_DIR = os.path.join(SCRIPT_DIR, "books")
BOOKS_TXT_DIR = os.environ.get("GUTENBERG_TXT_DIR", os.path.join(BOOKS_BASE_DIR, "txt"))
BOOKS_INDEX_DIR = os.path.join(BOOKS_BASE_DIR, "index")

HEADER_RE = re.compile(
    r'\*\*\* START OF (?:THIS |THE )?PROJECT GUTENBERG EBOOK.*?\*\*\*',
    re.IGNORECASE | re.DOTALL
)
FOOTER_RE = re.compile(
    r'\*\*\* END OF (?:THIS |THE )?PROJECT GUTENBERG EBOOK.*?',
    re.IGNORECASE | re.DOTALL
)

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
    cur.execute("DROP TABLE IF EXISTS gutenberg_paragraphs")
    cur.execute("""
        CREATE TABLE gutenberg_paragraphs (
            book_id integer,
            title text,
            author text,
            language string,
            start_char integer,
            body text
        ) morphology='stem_en,libstemmer_fr,libstemmer_de,libstemmer_it,libstemmer_es' min_stemming_len='2' index_sp='1'
    """)
    conn.commit()
    print("Table created.")

def bulk_insert(conn, rows):
    if not rows:
        return
    cur = conn.cursor()
    sql = ("INSERT INTO gutenberg_paragraphs "
           "(book_id,title,author,language,start_char,body) "
           "VALUES (%s,%s,%s,%s,%s,%s)")
    cur.executemany(sql, rows)
    conn.commit()

def get_zip_urls(lang):
    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    page_url = f"https://www.gutenberg.org/robot/harvest?filetypes[]=txt&langs[]={lang}"
    while page_url:
        print(f"  Fetching index: {page_url}")
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(page_url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                html = r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"  Warning: {e}")
            break
        
        # Find all URLs on the current page and get the count
        page_urls = re.findall(r'href="(https?://[^"]+\.zip)"', html)
        page_total = len(page_urls)

        # Yield the URL along with its position and the page total
        for i, url in enumerate(page_urls, 1):
            yield (url, i, page_total)
            
        nxt = re.search(r'href="(harvest[^"]+)"', html)
        if nxt:
            page_url = "https://www.gutenberg.org/robot/" + nxt.group(1).replace('&amp;', '&')
        else:
            page_url = None
        time.sleep(SLEEP_SEC)

def download_text(url):
    hdrs = {"User-Agent": "gutenberg-mcp-indexer/1.0"}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            data = r.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.endswith(".txt"):
                    raw_bytes = z.read(name)
                    header = raw_bytes[:2000].decode("ascii", errors="ignore")
                    enc_match = re.search(r'Character set encoding:\s*(\S+)', header)
                    encoding = enc_match.group(1) if enc_match else "utf-8"
                    try:
                        return raw_bytes.decode(encoding, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        return raw_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    skip {url}: {e}")
    return None

def extract_meta(raw, url):
    title   = re.search(r"Title:\s*(.+)",  raw)
    author  = re.search(r"Author:\s*(.+)", raw)
    book_id = re.search(r"/(\d+)\.zip",    url)
    return (
        title.group(1).strip()  if title  else "Unknown",
        author.group(1).strip() if author else "Unknown",
        int(book_id.group(1))   if book_id else 0,
    )

def strip_gutenberg(text):
    m = HEADER_RE.search(text)
    if m: text = text[m.end():]
    m = FOOTER_RE.search(text)
    if m: text = text[:m.start()]
    return text.strip()

def chunk_prose(text):
    paras, chunks, buf, offset, buf_start = re.split(r'\n\s*\n', text), [], "", 0, 0
    for para in paras:
        para = para.strip()
        if not para:
            offset += 2
            continue
        if len(buf) + len(para) > CHUNK_CHARS and buf:
            chunks.append((buf.strip(), buf_start))
            buf_start, buf = offset, ""
        buf += " " + para
        offset += len(para) + 2
    if buf.strip():
        chunks.append((buf.strip(), buf_start))
    return chunks

def main():
    print(f"\nConnecting to Manticore {MANTICORE_HOST}:{MANTICORE_PORT}...")
    try:
        conn = get_conn()
    except Exception as e:
        print(f"ERROR: Cannot connect: {e}")
        print("Make sure the ManticoreSearch service is running.")
        raise

    create_table(conn)

    total = 0
    for lang in LANGUAGES:
        print(f"\n-- Language: {lang} ------------------------------------")
        url_generator = get_zip_urls(lang)
        batch = []

        book_iterator = itertools.islice(url_generator, MAX_BOOKS) if MAX_BOOKS else url_generator

        for overall_count, (url, page_num, page_total) in enumerate(book_iterator, 1):
            print(f"  [Book {overall_count} | Page {page_num}/{page_total}] {url}")
            
            # --- Check if book exists before downloading ---
            temp_book_id_match = re.search(r"/(\d+)\.zip", url)
            if temp_book_id_match:
                temp_book_id = temp_book_id_match.group(1)
                if any(f"({temp_book_id})" in f or f.startswith(f"Unknown - Unknown ({lang})") for f in os.listdir(BOOKS_TXT_DIR)):
                    pass
                
            # --- Save book to disk -> books/txt ---
            os.makedirs(BOOKS_TXT_DIR, exist_ok=True)
            
            # We must download to get metadata for the filename
            raw = download_text(url)
            if not raw:
                continue

            title, author, book_id = extract_meta(raw, url)
            book_filename = f"{sanitize_filename(author)} - {sanitize_filename(title)} ({lang}).txt"
            book_path = os.path.join(BOOKS_TXT_DIR, book_filename)

            if os.path.exists(book_path):
                print(f"    Already exists: books/txt/{book_filename}")
                continue

            with open(book_path, "w", encoding="utf-8") as f:
                f.write(raw)
            print(f"    Saved: books/txt/{book_filename}")
            
            prose = strip_gutenberg(raw)
            if len(prose) < 200:
                continue

            for chunk_text, start_char in chunk_prose(prose):
                batch.append((book_id, title, author, lang, start_char, chunk_text))
                if len(batch) >= BATCH_SIZE:
                    bulk_insert(conn, batch)
                    total += len(batch)
                    print(f"    {total} paragraphs indexed...")
                    batch = []

            time.sleep(SLEEP_SEC)

        if batch:
            bulk_insert(conn, batch)
            total += len(batch)

    conn.close()
    print(f"\nDone! {total} total paragraphs indexed.")

if __name__ == "__main__":
    main()





