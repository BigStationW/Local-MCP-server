import os
import argparse
os.environ["TERM"] = "dumb"
os.environ["NO_COLOR"] = "1"
from PIL import Image as PILImage
import io
import asyncio
import logging
from datetime import datetime
from pathlib import Path
import httpx
from mcp.server.fastmcp.utilities.types import Image
from mcp.server.fastmcp import FastMCP, Image
from playwright.async_api import async_playwright
from starlette.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
import trafilatura
from ddgs import DDGS
from readability import Document
from markdownify import markdownify as md
import re
import atexit
import signal
from datetime import datetime
import certifi
import pymysql as _pymysql
import os as _os
import re as _re


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
SERVER_PORT = 4242  # overwritten at startup from CLI args

# ---------------------------------------------------------------------------
# SCREENSHOT STORAGE
# ---------------------------------------------------------------------------
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# SHARED HTTP CLIENT
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None

async def get_http_client() -> httpx.AsyncClient:
    """Get or create a shared HTTP client with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            http2=True,
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client

async def cleanup_http_client():
    """Close the shared HTTP client."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None

# ---------------------------------------------------------------------------
# MCP SERVER WITH CORS + STATIC FILE SERVING
# ---------------------------------------------------------------------------
class FastMCPWithCORS(FastMCP):
    def _build_extra_routes(self):
        async def serve_screenshot(request):
            filename = request.path_params["filename"]
            filepath = SCREENSHOT_DIR / filename
            if filepath.exists():
                return FileResponse(str(filepath))
            return JSONResponse({"error": "Not found"}, status_code=404)

        return [
            Route("/screenshots/{filename}", endpoint=serve_screenshot, methods=["GET"])
        ]

    def _add_cors(self, app: Starlette) -> Starlette:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"]
        )
        return app

    def streamable_http_app(self) -> Starlette:
        app = super().streamable_http_app()
        app.routes.extend(self._build_extra_routes())
        return self._add_cors(app)

    def sse_app(self, mount_path: str = "/") -> Starlette:
        app = super().sse_app(mount_path)
        app.routes.extend(self._build_extra_routes())
        return self._add_cors(app)

mcp = FastMCPWithCORS("Web-Tools-MCP", host="0.0.0.0")

# ---------------------------------------------------------------------------
# UTILITIES & BROWSER MANAGEMENT
# ---------------------------------------------------------------------------
class BrowserManager:
    """Manages the headless browser and sessions."""
    
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.sessions = {}
        self.counter = 0
        self._lock = asyncio.Lock()  # Prevent race conditions

    async def get_browser(self):
        """Get or create the browser instance with stealth features."""
        async with self._lock:
            if not self.playwright:
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ]
                )
            return self.browser
    
    async def create_stealth_page(self):
        """Create a page with anti-detection measures."""
        browser = await self.get_browser()
        page = await browser.new_page()
        
        # Set realistic user agent
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        
        # Hide webdriver property
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        return page
    
    async def cleanup(self):
        """Close all sessions and the browser."""
        for session_id, page in list(self.sessions.items()):
            try:
                await page.close()
            except:
                pass
        self.sessions.clear()
        
        if self.browser:
            await self.browser.close()
            self.browser = None
        
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

browser_manager = BrowserManager()

def parse_html_text(html_content: str, article_only: bool = True) -> str:
    if article_only:
        result = trafilatura.extract(
            html_content,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            no_fallback=False,
        )
        if result:
            return result.strip()
        # Fallback to readability+markdownify if trafilatura returns nothing
    
    # For article_only=False, or when trafilatura fails
    try:
        doc = Document(html_content)
        html_to_parse = doc.summary() if article_only else html_content
    except Exception:
        html_to_parse = html_content

    text = md(html_to_parse, heading_style="ATX",
              strip=["img", "script", "style", "nav", "footer", "header"])
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()

def normalize_image_bytes(data: bytes, source_fmt: str) -> tuple[bytes, str]:
    """
    Convert image bytes to JPEG if not already PNG/JPEG.
    llama.cpp's multimodal backend only supports PNG and JPEG.
    Returns (normalized_bytes, normalized_fmt).
    """
    source_fmt = source_fmt.lower().strip()
    if source_fmt in ("jpeg", "jpg", "png"):
        # PNG stays PNG, JPEG/JPG both become jpeg
        return data, "png" if source_fmt == "png" else "jpeg"

    # WebP, GIF, BMP, TIFF, AVIF, etc. → convert to JPEG
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            # Handle transparency (WebP/GIF/PNG with alpha → white background)
            if img.mode in ("RGBA", "LA", "P"):
                background = PILImage.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return buf.getvalue(), "jpeg"
    except Exception:
        # If conversion fails, return original and let the caller handle it
        return data, source_fmt

def save_screenshot(data: bytes, prefix: str = "screenshot") -> tuple[str, Image]:
    filename = f"{prefix}_{int(datetime.now().timestamp())}.png"
    filepath = SCREENSHOT_DIR / filename
    filepath.write_bytes(data)
    public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
    return public_url, Image(data=data, format="png")

_GUTENBERG_END_MARKERS = [
    "*** END OF THIS PROJECT GUTENBERG",
    "*** START: FULL LICENSE ***",
    "End of the Project Gutenberg EBook",
    "THE FULL PROJECT GUTENBERG LICENSE",
    "End of Project Gutenberg",
]
 
_GUTENBERG_START_MARKERS = [
    "*** START OF THIS PROJECT GUTENBERG",
    "*** START OF THE PROJECT GUTENBERG",
    "*END*THE SMALL PRINT!",
    "***START OF THE PROJECT GUTENBERG",
]

def _strip_gutenberg(text: str) -> str:
    # Fix 1: Strip BOM
    text = text.lstrip('\ufeff')

    # 1. Strip the end
    end_indices = [text.find(m) for m in _GUTENBERG_END_MARKERS if text.find(m) != -1]
    if end_indices:
        text = text[:min(end_indices)]

    # 2. Strip the start
    start_indices = [text.find(m) for m in _GUTENBERG_START_MARKERS if text.find(m) != -1]
    if start_indices:
        start_idx = min(start_indices)
        eol = text.find('\n', start_idx)
        text = text[eol + 1:] if eol != -1 else text[start_idx:]

    return text.strip()
 
def _manticore_conn():
    return _pymysql.connect(
        host="127.0.0.1", port=9306,
        user="", password="", database="",
        charset="utf8mb4", connect_timeout=5,
    )

# ---------------------------------------------------------------------------
# BASIC TOOLS
# ---------------------------------------------------------------------------
@mcp.tool()
async def sleep_timer(seconds: int = 0, milliseconds: int = 0) -> str:
    """Sleep for a period of time, useful to wait before continuing output."""
    total_time = seconds + (milliseconds / 1000.0)
    total_time = min(total_time, 45.0)
    await asyncio.sleep(total_time)
    return f"Slept for {total_time} seconds."

@mcp.tool()
def date_time() -> str:
    """Get the current date and time."""
    return datetime.now().strftime("%d/%m/%Y - %H:%M")

# ---------------------------------------------------------------------------
# LOCAL BOOK RETRIEVAL
# ----------------------------------------------------------------------------

@mcp.tool()
async def gutenberg_index_stats(language: str = "") -> str:
    """
    Return counts and book list from the Gutenberg full-text index.
 
    Call this BEFORE gutenberg_prose_search when:
    - You are unsure whether any books have been indexed.
    - Multiple searches return zero results and you suspect a connection/index issue.
    - You want to know which languages are available.
 
    Args:
        language: Optional two-letter code (e.g. "en", "fr"). Leave blank for all.
 
    Returns: Total paragraph count by language, list of indexed books, and a tip.
    """
    lang_clause = f"AND language='{language[:5].replace(chr(39), '')}'" if language else ""

    # --- Filesystem book count (always available, regardless of index state) ---
    books_dir = os.path.join(os.path.dirname(__file__), "gutenberg", "books", "txt")
    pattern = r"^(.+?) - (.+?) \((\w{2})\)\.txt$"
    disk_books = []
    if os.path.exists(books_dir):
        for f in os.listdir(books_dir):
            m = re.match(pattern, f)
            if m:
                _, _, lang = m.groups()
                disk_books.append(f)

    sql_total = f"SELECT COUNT(*) AS cnt FROM gutenberg_paragraphs WHERE 1=1 {lang_clause}"
    sql_langs = "SELECT language, COUNT(*) AS cnt FROM gutenberg_paragraphs GROUP BY language ORDER BY cnt DESC"
    sql_books = (
        f"SELECT book_id, title, author FROM gutenberg_paragraphs "
        f"WHERE 1=1 {lang_clause} GROUP BY book_id LIMIT 50"
    )
    sql_global_total = "SELECT COUNT(*) AS cnt FROM gutenberg_paragraphs"
    sql_lang_total = f"SELECT COUNT(*) AS cnt FROM gutenberg_paragraphs WHERE 1=1 {lang_clause}"
 
    try:
        conn = _manticore_conn()
        cur = conn.cursor(_pymysql.cursors.DictCursor)
 
        # Check global index first
        cur.execute(sql_global_total)
        global_total = cur.fetchone()["cnt"]

        # Then filtered total
        cur.execute(sql_lang_total)
        total = cur.fetchone()["cnt"]
 
        cur.execute(sql_langs)
        by_lang = cur.fetchall()
 
        cur.execute(sql_books)
        books = cur.fetchall()
 
        conn.close()
 
    except _pymysql.OperationalError as e:
        return (
            f"❌ Cannot connect to Manticore Search: {e}\n"
            f"Books on disk: {len(disk_books):,}\n"
            "Fix: Win+R → services.msc → ManticoreSearch → Start"
        )
    except Exception as e:
        return f"Manticore query error: {type(e).__name__}: {e}"
 
    lines = ["=== Gutenberg Index Stats ===\n"]
    lines.append(f"Books on disk: {len(disk_books):,}")
    lines.append(f"Total paragraphs indexed: {total:,}")
 
    if global_total == 0:
        lines.append(
            "\n⚠️  Search index is entirely empty — no paragraphs indexed.\n"
            f"However, {len(disk_books):,} book(s) are available on disk.\n"
            "Run your indexing pipeline to enable full-text search."
        )
        return "\n".join(lines)
 
    lines.append("\nParagraphs by language:")
    for row in by_lang:
        lines.append(f"  {row['language']}: {row['cnt']:,}")
 
    label = f" (language={language})" if language else ""
    lines.append(f"\nIndexed books{label}:")
    for b in books:
        lines.append(f"  [{b['book_id']}] {b['title']} — {b['author']}")
 
    lines.append(
        "\nTip: Use 2-4 words that would plausibly appear near each other "
        "in the middle of a sentence in these books.\n"
        "Tip: Pass a book_id from these results to list_available_books(book_id=...) "
        "to get the exact filename for read_book_content."
    )
    return "\n".join(lines)
 
@mcp.tool()
async def gutenberg_prose_search(
    query: str,
    language: str = "en",
    max_results: int = 5,
    proximity: int = 50,
) -> str:
    """
    Search the full prose text of all indexed Gutenberg books by concrete word clusters.
    Returns highlighted paragraphs with book IDs, filenames, and start_char offsets
    ready to pass directly to read_book_content.
 
    Args:
        query:       2-4 concrete words likely to appear near each other in prose.
                     Use mid-sentence fragments, NOT abstract mood words.
                     Good: "lamp brass shadow table" / "heart beat silence"
                     Bad:  "dark atmospheric sensual"
        language:    Two-letter code. Default "en".
        max_results: Paragraphs to return (default 5).
        proximity:   Max token distance between query words (default 50).
                     Increase to 100-200 if zero results with valid words.
 
    Workflow:
        gutenberg_index_stats()                    ← if unsure about the index
        gutenberg_prose_search(query="...")        ← find passages
        list_available_books(book_id=<id>)         ← resolve ID to filename
        read_book_content(filename=..., start_char=...)
    """
    clean_query = re.sub(r'[^\w\s]', '', query)
    words = [w.strip() for w in clean_query.split() if w.strip()]
    if not words:
        return "Empty query."
 
    lang_safe = language.replace("'", "")[:5]
    snip_terms = " ".join(words).replace("'", "''")
 
    def _make_sql(fts_expr: str) -> str:
        fts_safe = fts_expr.replace("'", "''")
        # Use [[ / ]] as delimiters — safe inside a single-quoted SQL string.
        # Post-process below replaces them with ** for readability.
        return (
            "SELECT book_id, title, author, language, start_char, "
            f"SNIPPET(body, '{snip_terms}', "
            f"'before_match=[[', 'after_match=]]', 'limit=400', 'around=15') AS snippet "
            "FROM gutenberg_paragraphs "
            f"WHERE MATCH('{fts_safe}') AND language='{lang_safe}' "
            f"LIMIT {int(max_results)} "
            "OPTION ranker=proximity_bm25, field_weights=(body=10,title=1)"
        )
 
    proximity_fts = (
        words[0] if len(words) == 1
        else f" NEAR/{int(proximity)} ".join(words)
    )
    fallback_fts = " ".join(words)
 
    try:
        conn = _manticore_conn()
        cur = conn.cursor(_pymysql.cursors.DictCursor)
 
        cur.execute(_make_sql(proximity_fts))
        rows = cur.fetchall()
        used_fallback = False
 
        if not rows and len(words) > 1:
            cur.execute(_make_sql(fallback_fts))
            rows = cur.fetchall()
            used_fallback = True
 
        conn.close()
 
    except _pymysql.OperationalError as e:
        return (
            f"❌ Cannot connect to Manticore Search: {e}\n"
            "Fix: Win+R → services.msc → ManticoreSearch → Start\n"
            "Call gutenberg_index_stats() to confirm the service is up."
        )
    except Exception as e:
        return f"Manticore query error: {type(e).__name__}: {e}"
 
    if not rows:
        return (
            f"No prose matches for '{query}' (language={language}, proximity={proximity}).\n\n"
            "Diagnosis:\n"
            "  1. Call gutenberg_index_stats() to confirm the index is non-empty.\n"
            "  2. Try fewer words — 2 is often better than 4.\n"
            "  3. Increase proximity= to 150 or 200.\n"
            "  4. Use words from the middle of sentences, not headings or dialogue tags.\n"
            f"  5. Confirm language='{language}' is indexed."
        )
 
    fallback_note = (
        f"\n⚠️  Proximity/{proximity} returned nothing — showing plain-match results "
        "(words appear in same paragraph but further apart than the proximity window). "
        "Consider increasing proximity= next time.\n"
        if used_fallback else ""
    )
 
    lines = [
        f"Found {len(rows)} prose match(es) for '{query}' "
        f"(language={language}, proximity={proximity}):{fallback_note}\n"
    ]
 
    for i, row in enumerate(rows, 1):
        # Replace [[ / ]] markers with ** for display
        raw_snippet = row.get("snippet") or ""
        snippet = raw_snippet.replace("[[", "**").replace("]]", "**")
        # Build a best-guess filename so the LLM can go straight to read_book_content
        guessed_filename = f"{row['author']} - {row['title']} ({row['language']}).txt"
        lines.append(
            f"{i}. {row['title']} by {row['author']}\n"
            f"   book_id: {row['book_id']} | start_char: {row['start_char']}\n"
            f"   filename: {guessed_filename}\n"
            f"   Match: ...{snippet[:500]}...\n"
        )
 
    lines.append(
        "Next steps:\n"
        "  • Confirm filename: list_available_books(book_id=<book_id>)\n"
        "  • Read passage:     read_book_content(filename=<filename>, start_char=<start_char>)"
    )
    return "\n".join(lines)
 
@mcp.tool()
async def list_available_books(
    author_filter: str = "",
    language: str = "",
    book_id: int = -1,
) -> str:
    """
    List all Gutenberg books available in the local txt directory.
 
    Args:
        author_filter: Substring to filter by author name (case-insensitive).
        language:      Two-letter language code, e.g. "en" or "fr".
        book_id:       Gutenberg book ID from gutenberg_prose_search or gutenberg_index_stats.
                       When provided, scans file headers and returns ONLY the book whose
                       Gutenberg ID matches — use this to get an exact filename before
                       calling read_book_content or get_book_stats.
 
    Returns: Book list with exact filenames.
 
    Typical use after a search:
        gutenberg_prose_search → book_id: 1898
        list_available_books(book_id=1898) → filename: "Honore de Balzac - Albert Savarus (en).txt"
        read_book_content(filename=..., start_char=...)
    """
    books_dir = os.path.join(os.path.dirname(__file__), "gutenberg", "books", "txt")
 
    if not os.path.exists(books_dir):
        return f"Books directory not found: {books_dir}"
 
    txt_files = [f for f in os.listdir(books_dir) if f.endswith(".txt")]
    if not txt_files:
        return "No books found in the directory."
 
    pattern = r"^(.+?) - (.+?) \((\w{2})\)\.txt$"
    books = []
 
    for filename in txt_files:
        m = re.match(pattern, filename)
        if not m:
            continue
        author, title, lang = m.groups()
        if author_filter and author_filter.lower() not in author.lower():
            continue
        if language and lang != language:
            continue
        books.append({"filename": filename, "author": author, "title": title, "language": lang})
 
    # book_id filter: scan each candidate file's header for the Gutenberg etext number
    if book_id != -1:
        id_pattern = re.compile(rf"(?:E[Tt]ext|[Ee]Book)[^\d]*{book_id}\b", re.IGNORECASE)
        matched = []
        for book in books:
            fpath = os.path.join(books_dir, book["filename"])
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    header = f.read(1500)   # ID always appears in the first ~1 KB
                if id_pattern.search(header):
                    matched.append(book)
            except Exception:
                continue
        if not matched:
            return (
                f"No book found with book_id={book_id}.\n"
                "The ID comes from gutenberg_prose_search or gutenberg_index_stats.\n"
                "If you're certain the book is in the library, call list_available_books() "
                "without a book_id to see all available filenames."
            )
        books = matched
 
    if not books:
        filters = []
        if author_filter:
            filters.append(f"author containing '{author_filter}'")
        if language:
            filters.append(f"language='{language}'")
        if book_id != -1:
            filters.append(f"book_id={book_id}")
        return f"No books found matching {' and '.join(filters)}."
 
    books.sort(key=lambda x: (x["author"], x["title"]))
 
    lines = [f"Found {len(books)} book(s):\n"]
    for i, book in enumerate(books, 1):
        lines.append(f"{i}. {book['title']} by {book['author']} ({book['language']})")
        lines.append(f"   Filename: {book['filename']}\n")
 
    return "\n".join(lines)
 
@mcp.tool()
async def get_book_stats(filename: str) -> str:
    """
    Get metadata, statistics, and chapter offsets for a specific book.
 
    Character counts and chapter offsets reflect story content only —
    Gutenberg boilerplate and license text are automatically excluded.
 
    Args:
        filename: Exact filename from list_available_books,
                  e.g., "Honore de Balzac - Albert Savarus (en).txt"
 
    Returns:
        Metadata, character/word/line counts, reading time, 500-char preview,
        and a table of chapter offsets for use with read_book_content.
    """
    books_dir = os.path.join(os.path.dirname(__file__), "gutenberg", "books", "txt")
    filepath = os.path.join(books_dir, filename)
 
    if not os.path.exists(filepath):
        return f"Book not found: {filename}\nUse list_available_books() to see available books."
 
    pat = r"^(.+?) - (.+?) \((\w{2})\)\.txt$"
    m = re.match(pat, filename)
    author, title, lang = m.groups() if m else ("Unknown", "Unknown", "??")
 
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        return f"Error reading book: {type(e).__name__}: {e}"
 
    # Strip boilerplate FIRST so all stats reflect story content only
    content = _strip_gutenberg(raw)
 
    char_count  = len(content)
    word_count  = len(content.split())
    line_count  = content.count("\n") + 1
    reading_min = word_count / 250
 
    preview = content[:500].strip() + ("..." if char_count > 500 else "")
 
    # Detect chapter headings — exclude known boilerplate section titles
    _EXCLUDE_TERMS = {
        "GUTENBERG", "LICENSE", "ADDENDUM", "SECTION", "FOUNDATION",
        "INFORMATION", "DONATIONS", "MISSION",
    }
 
    heading_re = re.compile(
        r"^(?:"
        r"(?:CHAPTER|Chapter|PART|Part|BOOK|Book|SECTION|Section)\s+[\w]+[^\n]*"
        r"|[IVXivx]{1,6}\.\s+[A-Z][^\n]+"
        r"|[A-Z][A-Z\s]{4,50}"
        r")$",
        re.MULTILINE,
    )
 
    chapters = []
    for match in heading_re.finditer(content):
        heading = match.group(0).strip()
        # Skip if any boilerplate term appears in the heading
        if any(term in heading.upper() for term in _EXCLUDE_TERMS):
            continue
        if len(heading) < 3:
            continue
        chapters.append((match.start(), heading))
 
    # Deduplicate hits within 50 chars of each other
    deduped = []
    for offset, text in chapters:
        if deduped and offset - deduped[-1][0] < 50:
            continue
        deduped.append((offset, text))
 
    if deduped:
        ch_lines = [f"\nDetected {len(deduped)} chapter/section offset(s):"]
        for offset, heading in deduped[:40]:
            ch_lines.append(f"  char {offset:>8,} — {heading[:80]}")
        if len(deduped) > 40:
            ch_lines.append(f"  ... and {len(deduped) - 40} more.")
        ch_lines.append(
            "\nPass any start_char to read_book_content() to begin at that chapter."
        )
        chapters_section = "\n".join(ch_lines)
    else:
        chapters_section = (
            "\nNo chapter headings detected. Use start_char=0 to read from the beginning, "
            "or use a start_char from gutenberg_prose_search results."
        )
 
    return (
        f"Book: {title}\n"
        f"Author: {author}\n"
        f"Language: {lang}\n"
        f"Filename: {filename}\n\n"
        f"Statistics (story content only, boilerplate excluded):\n"
        f"  Characters:        {char_count:,}\n"
        f"  Words:             {word_count:,}\n"
        f"  Lines:             {line_count:,}\n"
        f"  Est. reading time: {reading_min:.1f} min\n\n"
        f"Preview (first 500 chars of story):\n{preview}"
        f"{chapters_section}"
    )
 
@mcp.tool()
async def read_book_content(
    filename: str,
    start_char: int = 0,
    end_char: int = -1,
    max_chars: int = 3000,
    align_to_paragraph: bool = True,
) -> str:
    """
    Read a passage from a Gutenberg book.
 
    Gutenberg license text and boilerplate are automatically stripped —
    you will never receive license content regardless of start_char.
 
    Args:
        filename:           Exact filename from list_available_books.
        start_char:         Starting character position (default 0 = beginning of story).
                            Use offsets from get_book_stats or gutenberg_prose_search.
        end_char:           Ending position. Default -1 = start_char + max_chars.
        max_chars:          Maximum characters to return (default 3 000).
                            Increase deliberately when you need a longer passage.
                            Set to 0 for no limit (use with care).
        align_to_paragraph: If True (default), walk start_char back up to 500 chars
                            to find the nearest blank line, so the passage always
                            starts at a clean paragraph boundary.
 
    Returns: The passage with position metadata and a "continue" hint.
    """
    books_dir = os.path.join(os.path.dirname(__file__), "gutenberg", "books", "txt")
    filepath  = os.path.join(books_dir, filename)
 
    if not os.path.exists(filepath):
        available = (
            [f for f in os.listdir(books_dir) if f.endswith(".txt")]
            if os.path.exists(books_dir) else []
        )
        hint = (
            "\nAvailable books:\n" + "\n".join(f"  - {f}" for f in available[:5])
            if available else ""
        )
        return f"Book not found: {filename}{hint}"
 
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        return f"Error reading book: {type(e).__name__}: {e}"
 
    # Strip boilerplate FIRST — offsets now refer to clean story text
    content      = _strip_gutenberg(raw)
    total_length = len(content)
    start_char   = max(0, start_char)
 
    # Clamp start_char to story length (prevents reading into stripped territory)
    if start_char >= total_length:
        return (
            f"start_char={start_char:,} is beyond the end of the story "
            f"({total_length:,} chars after boilerplate is removed).\n"
            "Use get_book_stats() to see the actual story length and chapter offsets."
        )
 
    # Walk back to nearest paragraph boundary
    if align_to_paragraph and start_char > 0:
        look_back   = max(0, start_char - 500)
        segment     = content[look_back:start_char]
        last_blank  = segment.rfind("\n\n")
        if last_blank != -1:
            start_char = look_back + last_blank + 2
 
    # Resolve end_char
    if end_char == -1 or end_char <= start_char:
        end_char = start_char + (max_chars if max_chars > 0 else total_length)
    end_char = min(end_char, total_length)
 
    actual_length = end_char - start_char
 
    if max_chars > 0 and actual_length > max_chars:
        return (
            f"Requested passage ({actual_length:,} chars) exceeds max_chars={max_chars:,}.\n"
            "Narrow end_char, or increase max_chars if you deliberately want a longer read.\n"
            f"Story length (boilerplate excluded): {total_length:,} chars.\n"
            f"Tip: get_book_stats('{filename}') shows chapter offsets."
        )
 
    if start_char >= end_char:
        return f"Invalid range: start_char ({start_char:,}) >= end_char ({end_char:,})"
 
    passage = content[start_char:end_char]
 
    return "\n".join([
        f"Book: {filename}",
        f"Passage: chars {start_char:,}–{end_char:,} ({actual_length:,} chars)",
        f"Story length (boilerplate excluded): {total_length:,} chars",
        f"\n{'=' * 60}\n",
        passage,
        f"\n{'=' * 60}",
        f"End of passage (chars {start_char:,}–{end_char:,} of {total_length:,})",
        f"To continue reading: read_book_content(filename='{filename}', start_char={end_char})",
    ])

# ---------------------------------------------------------------------------
# HTTP / WEB TOOLS
# ---------------------------------------------------------------------------
@mcp.tool()
async def image_search(query: str, max_results: int = 5) -> list:
    """
    Search for images and return them so the model can see them directly.
    Uses DuckDuckGo image search to find direct image URLs, then downloads them.
    Much more efficient than web_search + navigate + screenshot for finding images.
    """
    try:
        results = []
        with DDGS() as ddgs:
            hits = list(ddgs.images(query, max_results=max_results))

        if not hits:
            return ["No image results found."]

        client = await get_http_client()
        out = []
        downloaded = 0
        batch_ts = int(datetime.now().timestamp()) 

        for hit in hits:
            image_url = hit.get("image", "")
            title = hit.get("title", "No title")
            source = hit.get("url", "")

            if not image_url:
                continue

            try:
                resp = await client.get(image_url, timeout=10)
                resp.raise_for_status()

                mime = resp.headers.get("content-type", "image/jpeg")
                fmt = mime.split("/")[-1].split(";")[0].strip() or "jpeg"
                # Normalize weird formats
                if fmt not in ("png", "jpeg", "jpg", "gif", "webp"):
                    fmt = "jpeg"

                normalized_data, normalized_fmt = normalize_image_bytes(resp.content, fmt)
                filename = f"imgsearch_{batch_ts}_{downloaded}.{normalized_fmt}"
                filepath = SCREENSHOT_DIR / filename
                filepath.write_bytes(normalized_data)

                public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
                img = Image(data=normalized_data, format=normalized_fmt)

                out.append(f'Result {downloaded + 1}: "{title}" (source page: {source})')
                out.append(f"![{title}]({public_url})")
                out.append(img)
                
                downloaded += 1
                if downloaded >= max_results:
                    break

            except Exception:
                # Silently skip images that fail to download (dead links, hotlink protection, etc.)
                continue

        if not out:
            return ["Found results but could not download any images (hotlink protection or dead links). Try puppeteer_screenshot on one of these pages instead."] + \
                   [f"- {h.get('title','')}: {h.get('image','')}" for h in hits[:5]]

        return out

    except Exception as e:
        return [f"Image search error: {str(e)}"]


@mcp.tool()
async def puppeteer_session_find_images(
    session_id: str,
    min_width: int = 200,
    min_height: int = 200,
    limit: int = 10,
) -> list:
    """
    Extract direct image URLs from the current page of a session.
    Filters by minimum dimensions so you get actual content images, not icons/logos.
    Returns a list of src URLs + downloads the top image so the model can see it.
    Use this after navigating to an art page (DeviantArt, ArtStation, Pixiv, etc.)
    to grab the real artwork URL instead of screenshotting the entire page.
    """
    page = browser_manager.sessions.get(session_id)
    if not page:
        return [f"Error: No session found with session_id '{session_id}'."]

    try:
        batch_ts = int(datetime.now().timestamp())
        images = await page.evaluate(f"""
            () => {{
                const imgs = Array.from(document.querySelectorAll('img'));
                return imgs
                    .filter(img => img.naturalWidth >= {min_width} && img.naturalHeight >= {min_height})
                    .map(img => ({{
                        src: img.src || img.currentSrc || '',
                        alt: img.alt || '',
                        width: img.naturalWidth,
                        height: img.naturalHeight
                    }}))
                    .filter(img => img.src && img.src.startsWith('http'))
                    .sort((a, b) => (b.width * b.height) - (a.width * a.height))
                    .slice(0, {limit});
            }}
        """)

        if not images:
            return [f"No images found with minimum size {min_width}x{min_height}px."]

        out = [f"Found {len(images)} image(s) on page:\n"]
        for i, img in enumerate(images, 1):
            out.append(f"{i}. {img['src']}\n   Alt: {img['alt']} | Size: {img['width']}x{img['height']}px")

        # Auto-download the largest image so the model can see it
        client = await get_http_client()
        top = images[0]
        try:
            resp = await client.get(top["src"], timeout=10)
            resp.raise_for_status()
            mime = resp.headers.get("content-type", "image/jpeg")
            fmt = mime.split("/")[-1].split(";")[0].strip() or "jpeg"
            if fmt not in ("png", "jpeg", "jpg", "gif", "webp"):
                fmt = "jpeg"

            normalized_data, normalized_fmt = normalize_image_bytes(resp.content, fmt)

            filename = f"pageimg_{session_id}_{batch_ts}.{normalized_fmt}"
            filepath = SCREENSHOT_DIR / filename
            filepath.write_bytes(normalized_data)

            public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
            img_obj = Image(data=normalized_data, format=normalized_fmt)

            out.append(f"\n![{top['alt']}]({public_url})")
            out.append(img_obj)
        except Exception as e:
            out.append(f"\nCould not auto-download largest image: {e}")

        return out

    except Exception as e:
        return [f"Error extracting images: {str(e)}"]

@mcp.tool()
async def http_get_text(
    url: str,
    user_agent: str = None,
    referer: str = None,
    article_only: bool = True,
    max_chars: int = 5000,
) -> str:
    """Fetch a URL via HTTP GET and extract readable plain text.
    Set max_chars to 0 for unlimited text length.
    Set article_only=True for main content extraction; use Playwright tools for JS-rendered sites."""
    headers = {
        "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if referer:
        headers["Referer"] = referer

    client = await get_http_client()
    try:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        text = parse_html_text(resp.text, article_only=article_only)
        
        # If max_chars is 0 or less, return everything. Otherwise, slice it.
        if max_chars <= 0:
            return text
        return text[:max_chars]
    except Exception as e:
        return f"Error fetching URL: {str(e)}"

@mcp.tool()
async def http_get_image(url: str, user_agent: str = None) -> list:
    """Download an image and display it — model can now actually see it."""
    headers = {
        "User-Agent": user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    client = await get_http_client()
    try:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()

        mime_type = resp.headers.get("content-type", "image/jpeg")
        fmt = mime_type.split("/")[-1].split(";")[0] if "/" in mime_type else "jpeg"

        normalized_data, normalized_fmt = normalize_image_bytes(resp.content, fmt)

        filename = f"image_{int(datetime.now().timestamp())}.{normalized_fmt}"
        filepath = SCREENSHOT_DIR / filename
        filepath.write_bytes(normalized_data)

        public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
        img = Image(data=normalized_data, format=normalized_fmt)

        return [f"![image]({public_url})", img]
    except Exception as e:
        return [f"Error downloading image: {str(e)}"]

@mcp.tool()
async def web_search(query: str, max_results: int = 10, follow_top_links: int = 0) -> str:
    """Search the web. Set follow_top_links > 0 to also fetch full content from top N results."""
    try:
        results = []
        urls_to_fetch = []
        with DDGS() as ddgs:
            for i, result in enumerate(ddgs.text(query, max_results=max_results), 1):
                title = result.get("title", "No title")
                url = result.get("href", "")
                snippet = result.get("body", "")
                results.append(f"{i}. {title}\n   URL: {url}\n   {snippet}\n")
                if i <= follow_top_links and url:
                    urls_to_fetch.append((i, url))

        # Fetch full content from top N pages
        if urls_to_fetch:
            client = await get_http_client()
            for i, url in urls_to_fetch:
                try:
                    resp = await client.get(url, timeout=10)
                    text = parse_html_text(resp.text, article_only=True)
                    results.append(f"\n--- Full content of result #{i} ({url}) ---\n{text[:3000]}\n")
                except Exception as e:
                    results.append(f"\n--- Could not fetch result #{i}: {e} ---\n")

        return "\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Search error: {str(e)}"
    
@mcp.tool()
async def puppeteer_session_find_links(session_id: str, search_text: str) -> str:
    """Find all links associated with elements containing specific text.
    Works correctly for forums like 4chan where titles are in spans, not anchor text.
    """
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."

    try:
        links = await page.evaluate(f"""
            () => {{
                const searchText = {repr(search_text.lower())};
                const results = new Map();

                // Search ALL elements for matching text
                const allElements = document.querySelectorAll('*');
                for (const el of allElements) {{
                    // Only check text nodes directly in this element (not children)
                    const directText = Array.from(el.childNodes)
                        .filter(n => n.nodeType === Node.TEXT_NODE)
                        .map(n => n.textContent)
                        .join('');
                    
                    if (directText.toLowerCase().includes(searchText)) {{
                        // Walk up to find the nearest ancestor <a> tag
                        let node = el;
                        let anchor = null;
                        while (node && node !== document.body) {{
                            if (node.tagName === 'A' && node.href) {{
                                anchor = node;
                                break;
                            }}
                            // Also check siblings and children for anchors
                            const childAnchor = node.querySelector('a[href]');
                            if (childAnchor) {{
                                anchor = childAnchor;
                                break;
                            }}
                            node = node.parentElement;
                        }}
                        if (anchor && !results.has(anchor.href)) {{
                            results.set(anchor.href, {{
                                text: el.textContent.trim().slice(0, 120),
                                href: anchor.href
                            }});
                        }}
                    }}
                }}

                return Array.from(results.values()).slice(0, 20);
            }}
        """)

        if not links:
            return f"No links found containing '{search_text}'"

        result = f"Found {len(links)} links containing '{search_text}':\n\n"
        for i, link in enumerate(links, 1):
            result += f"{i}. {link['text']}\n   {link['href']}\n\n"
        return result
    except Exception as e:
        return f"Error finding links: {str(e)}"

# ---------------------------------------------------------------------------
# PLAYWRIGHT (HEADLESS BROWSER) TOOLS
# ---------------------------------------------------------------------------
@mcp.tool()
async def puppeteer_screenshot(
    url: str,
    wait_until: str = "networkidle",
    wait_for_selector: str = None,
) -> list:
    """Take a full-page screenshot of a webpage. Returns the image so the model can see it."""
    page = await browser_manager.create_stealth_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=30000)
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector, timeout=15000)
        data = await page.screenshot(full_page=True)
        public_url, img = save_screenshot(data)
        return [f"![screenshot]({public_url})", img]
    except Exception as e:
        return [f"Screenshot error: {str(e)}"]
    finally:
        await page.close()

@mcp.tool()
async def puppeteer_session_create(url: str, wait_until: str = "networkidle") -> str:
    """Create a persistent browser session. Returns a session_id for future calls."""
    try:
        page = await browser_manager.create_stealth_page()

        browser_manager.counter += 1
        session_id = f"session_{browser_manager.counter}"
        browser_manager.sessions[session_id] = page

        await page.goto(url, wait_until=wait_until, timeout=30000)
        return f"Session created. session_id: {session_id}"
    except Exception as e:
        return f"Error creating session: {str(e)}"

@mcp.tool()
async def puppeteer_session_screenshot(session_id: str) -> list:
    """Take a screenshot of a running session. Returns the image so the model can see it."""
    page = browser_manager.sessions.get(session_id)
    if not page:
        return [f"Error: No session found with session_id '{session_id}'."]

    try:
        data = await page.screenshot(full_page=True)
        public_url, img = save_screenshot(data, prefix=session_id)
        return [f"![screenshot]({public_url})", img]
    except Exception as e:
        return [f"Screenshot error: {str(e)}"]

@mcp.tool()
async def puppeteer_session_navigate(
    session_id: str,
    url: str,
    wait_until: str = "networkidle",
    wait_for_selector: str = None
) -> str:
    """Navigate an existing session to a new URL."""
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."

    try:
        await page.goto(url, wait_until=wait_until, timeout=30000)
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector, timeout=15000)
        return f"Successfully navigated to {url}"
    except Exception as e:
        return f"Navigation error: {str(e)}"

@mcp.tool()
async def puppeteer_session_get_page_text(
    session_id: str, 
    extract_article_only: bool = False,
    max_chars: int = 5000
) -> str:
    """Get the current page text from an existing session.
    
    Args:
        session_id: The session ID
        extract_article_only: Set to True to extract only main article content (strips nav/menus).
                              Default False returns all page text (better for forums/listings).
        max_chars: Maximum characters to return. Set to 0 for unlimited.
    """
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."
    try:
        content = await page.content()
        text = parse_html_text(content, article_only=extract_article_only)
        
        if max_chars <= 0:
            return text
        return text[:max_chars]
    except Exception as e:
        return f"Error getting page text: {str(e)}"

@mcp.tool()
async def puppeteer_session_close(session_id: str) -> str:
    """Close and destroy a browser session."""
    page = browser_manager.sessions.pop(session_id, None)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."
    try:
        await page.close()
        return f"Session {session_id} closed successfully."
    except Exception as e:
        return f"Error closing session: {str(e)}"

@mcp.tool()
async def puppeteer_session_click(session_id: str, selector: str) -> str:
    """Click an element in an existing session."""
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."

    try:
        await page.click(selector, timeout=10000)
        return f"Clicked element: {selector}"
    except Exception as e:
        return f"Click error: {str(e)}"

@mcp.tool()
async def puppeteer_session_type(session_id: str, selector: str, text: str) -> str:
    """Type text into an element in an existing session."""
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."

    try:
        await page.fill(selector, text)
        return f"Typed into {selector}: {text}"
    except Exception as e:
        return f"Type error: {str(e)}"

@mcp.tool()
async def puppeteer_session_evaluate(session_id: str, script: str) -> str:
    """
    Execute arbitrary JavaScript in the page and return the result as a string.
    Extremely useful for extracting specific DOM attributes, href values, data
    attributes, etc. that no other tool exposes.
    """
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."
    try:
        result = await page.evaluate(script)
        if result is None:
            return "null (element not found or script returned nothing)"
        # Serialize non-string results
        if not isinstance(result, str):
            import json
            return json.dumps(result, ensure_ascii=False, indent=2)
        return result
    except Exception as e:
        return f"JavaScript evaluation error: {str(e)}"
    
@mcp.tool()
async def puppeteer_session_get_element_html(
    session_id: str,
    selector: str,
    outer: bool = True,
) -> str:
    """
    Return the innerHTML (or outerHTML) of the first element matching `selector`.
    Much cheaper than getting the full page HTML when you only care about one section.
    
    Args:
        selector: CSS selector, e.g. '.postContainer', 'article'
        outer:    True  → includes the element's own tag (outerHTML)
                  False → only the element's children (innerHTML)
    """
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."
    try:
        prop = "outerHTML" if outer else "innerHTML"
        html = await page.evaluate(
            f"el => el ? el.{prop} : null",
            await page.query_selector(selector)
        )
        if html is None:
            return f"No element found for selector: {selector}"
        return html[:20000]   # cap so it doesn't blow the context
    except Exception as e:
        return f"Error getting element HTML: {str(e)}"
    
@mcp.tool()
async def puppeteer_session_get_page_html(
    session_id: str,
    max_chars: int = 30000,
) -> str:
    """
    Return the current fully-rendered HTML of the page (after JS has run).
    Use get_element_html with a narrow selector instead when possible —
    this can be very large. Set max_chars to 0 for unlimited.
    """
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."
    try:
        content = await page.content()
        if max_chars <= 0:
            return content
        return content[:max_chars]
    except Exception as e:
        return f"Error getting page HTML: {str(e)}"

# ---------------------------------------------------------------------------
# SERVER RUNNER
# ---------------------------------------------------------------------------
def sync_cleanup():
    """Synchronous cleanup for atexit/signal handlers."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.wait_for(browser_manager.cleanup(), timeout=3.0))
    except Exception:
        pass
    finally:
        loop.close()

atexit.register(sync_cleanup)

# Also handle SIGTERM explicitly (what the X button sends)
signal.signal(signal.SIGTERM, lambda *_: sync_cleanup())

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Web Tools MCP Server")
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=4242,
        help="Port to run the server on (default: 4242)"
    )
    args = parser.parse_args()
    
    # Set the global port
    SERVER_PORT = args.port
    mcp.settings.port = args.port
    
    # Disable colors in Uvicorn
    import uvicorn
    if "default" in uvicorn.config.LOGGING_CONFIG["formatters"]:
        uvicorn.config.LOGGING_CONFIG["formatters"]["default"]["use_colors"] = False
    if "access" in uvicorn.config.LOGGING_CONFIG["formatters"]:
        uvicorn.config.LOGGING_CONFIG["formatters"]["access"]["use_colors"] = False
    
    print(f"Web Tools MCP Server is running!")
    print(f"Connect your AI client at: http://localhost:{SERVER_PORT}/mcp")
    
    mcp.run(transport="streamable-http")
