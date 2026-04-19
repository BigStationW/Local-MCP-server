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

# Load Gutenberg catalog once at startup (optional)
_GUTENBERG_CATALOG = {}

def sanitize_filename(s: str) -> str:
    """Match the filename sanitization logic used by the indexer."""
    s = re.sub(r'[<>:"/\\|?*]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:80]

def _load_gutenberg_catalog():
    """Load pg_catalog.csv into memory for fast book_id → filename lookups."""
    global _GUTENBERG_CATALOG
    if _GUTENBERG_CATALOG:
        return
    
    catalog_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "gutenberg", "books", "pg_catalog.csv"
    )
    
    if not os.path.exists(catalog_path):
        logging.debug("Gutenberg catalog not found")
        return
    
    try:
        import csv
        with open(catalog_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                book_id = int(row.get('Text#', 0) or 0)
                if not book_id:
                    continue
                
                # Use robust .get() calls and sanitization identical to indexer
                author_raw = row.get('Authors', 'Unknown').strip() or 'Unknown'
                title_raw = row.get('Title', 'Unknown').strip() or 'Unknown'
                lang = row.get('Language', '').strip().lower()

                author_sanitized = sanitize_filename(author_raw)
                title_sanitized = sanitize_filename(title_raw)
                
                # Build filename in the exact same format as the indexer
                filename = f"{author_sanitized} - {title_sanitized} ({lang}).txt"
                _GUTENBERG_CATALOG[book_id] = filename

        logging.info(f"Loaded {len(_GUTENBERG_CATALOG)} books from Gutenberg catalog")
    except Exception as e:
        logging.warning(f"Could not load Gutenberg catalog: {e}")

# Load catalog at module import time
_load_gutenberg_catalog()

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

def _strip_gutenberg(text: str) -> tuple[str, int]:
    """
    Strip Gutenberg boilerplate from raw text.

    Returns:
        (story_content, strip_offset) where strip_offset is the number of
        characters removed from the front of the original text. All offsets
        stored in the index (raw-file positions) can be translated to
        story-content positions by subtracting strip_offset.
    """
    # Strip BOM
    stripped = text.lstrip('\ufeff')
    bom_len = len(text) - len(stripped)
    text = stripped

    # Strip the end
    end_indices = [text.find(m) for m in _GUTENBERG_END_MARKERS if text.find(m) != -1]
    if end_indices:
        text = text[:min(end_indices)]

    # Strip the start and record how many chars were removed from the front
    strip_offset = bom_len
    start_indices = [text.find(m) for m in _GUTENBERG_START_MARKERS if text.find(m) != -1]
    if start_indices:
        start_idx = min(start_indices)
        eol = text.find('\n', start_idx)
        if eol != -1:
            strip_offset += eol + 1
            text = text[eol + 1:]
        else:
            strip_offset += start_idx
            text = text[start_idx:]

    lstripped = text.lstrip()
    strip_offset += len(text) - len(lstripped)
    text = lstripped.rstrip()

    return text, strip_offset
 
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
async def gutenberg_prose_search(
    query: str,
    language: str = "en",
    max_results: int = 10,
    offset: int = 0,
    proximity: int = 50,
) -> str:
    """
    Search the full prose text of all indexed Gutenberg books by concrete word clusters.
    Returns highlighted paragraphs with book IDs, filenames, and start_char offsets
    ready to pass directly to read_book_content.

    Results are ranked by proximity_bm25: a combined score of
      • BM25  — rewards rare words that appear frequently in the paragraph
      • Proximity — rewards paragraphs where the query words sit close together
    Body text is weighted 10× higher than title text.
    The first result is therefore the strongest match; quality degrades toward the end.

    Args:
        query:       2-4 concrete words likely to appear near each other in prose.
                     Use mid-sentence fragments, NOT abstract mood words.
                     Good: "lamp brass shadow table" / "heart beat silence"
                     Bad:  "dark atmospheric sensual"
        language:    Two-letter code. Default "en".
        max_results: Number of paragraphs to return per page (default 10).
        offset:      Zero-based index of the first result to return (default 0).
                     Use offset=10 to get results 11-20, offset=20 for 21-30, etc.
        proximity:   Max token distance between query words (default 50).
                     Increase to 100-200 if zero results with valid words.

    Workflow:
        gutenberg_prose_search(query="...")                          ← first page
        gutenberg_prose_search(query="...", offset=10)              ← next page
        read_book_content(filename=..., start_char=...)
    """
    clean_query = re.sub(r'[^\w\s]', '', query)
    words = [w.strip() for w in clean_query.split() if w.strip()]
    if not words:
        return "Empty query."
 
    lang_safe = language.replace("'", "")[:5]
 
    def _make_sql(fts_expr: str) -> str:
        fts_safe = fts_expr.replace("'", "''")
        return (
            "SELECT book_id, title, author, language, start_char, body "
            "FROM gutenberg_paragraphs "
            f"WHERE MATCH('{fts_safe}') AND language='{lang_safe}' "
            f"LIMIT {int(offset)}, {int(max_results)} "
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
        cur.execute("SHOW META")
        meta = {r["Variable_name"]: r["Value"] for r in cur.fetchall()}
        total_found = int(meta.get("total_found", len(rows)))
        used_fallback = False
 
        if not rows and len(words) > 1:
            cur.execute(_make_sql(fallback_fts))
            rows = cur.fetchall()
            cur.execute("SHOW META")
            meta = {r["Variable_name"]: r["Value"] for r in cur.fetchall()}
            total_found = int(meta.get("total_found", len(rows)))
            used_fallback = True
 
        conn.close()
 
    except _pymysql.OperationalError as e:
        return (
            f"❌ Cannot connect to Manticore Search: {e}\n"
            "Fix: Win+R → services.msc → ManticoreSearch → Start"
        )
    except Exception as e:
        return f"Manticore query error: {type(e).__name__}: {e}"
 
    if not rows:
        return (
            f"No prose matches for '{query}' (language={language}, proximity={proximity}).\n\n"
            "Diagnosis:\n"
            "  1. Try fewer words — 2 is often better than 4.\n"
            "  2. Increase proximity= to 150 or 200.\n"
            "  3. Use words from the middle of sentences, not headings or dialogue tags.\n"
            f"  4. Confirm language='{language}' is indexed."
        )
 
    fallback_note = (
        f"\n⚠️  Proximity/{proximity} returned nothing — showing plain-match results "
        "(words appear in same paragraph but further apart than the proximity window). "
        "Consider increasing proximity= next time.\n"
        if used_fallback else ""
    )
 
    first = offset + 1
    last  = offset + len(rows)
    lines = [
        f"Found {total_found} prose match(es) for '{query}' "
        f"(language={language}, proximity={proximity}) — displaying {first}–{last}:"
        f"{fallback_note}\n"
    ]

    for i, row in enumerate(rows, 1):
        body = (row.get("body") or "").strip()

        if len(body) <= 400:
            snippet = body
        else:
            # Find the first sentence-ending punctuation AT or AFTER char 400
            match = re.search(r'[.!?]', body[400:])
            if match:
                snippet = body[:400 + match.start() + 1]
            else:
                snippet = body[:400].rsplit(' ', 1)[0] + '…' 

        filename = _GUTENBERG_CATALOG.get(row['book_id'])

        if filename:
            lines.append(
                f"{i}. {row['title']} by {row['author']}\n"
                f"   book_id: {row['book_id']} | start_char: {row['start_char']}\n"
                f"   filename: {filename}\n"
                f"   Match: {snippet}\n"
            )
        else:
            lines.append(
                f"{i}. {row['title']} by {row['author']}\n"
                f"   book_id: {row['book_id']} | start_char: {row['start_char']}\n"
                f"   Match: {snippet}\n"
            )

    next_offset = offset + len(rows)
    prev_offset = max(0, offset - max_results)

    next_steps = ["\nNext steps:"]
    if next_offset < total_found:
        next_steps.append(
            f"  • Continue  → gutenberg_prose_search(query='{query}', offset={next_offset}, max_results={max_results})"
        )
    if offset > 0:
        next_steps.append(
            f"  • Go back   → gutenberg_prose_search(query='{query}', offset={prev_offset}, max_results={max_results})"
        )
    if _GUTENBERG_CATALOG:
        next_steps.append(
            "  • Read text → read_book_content(filename=<filename>, start_char=<start_char>)"
        )
    else:
        next_steps.append(
            "  • Read text → read_book_content(filename=<filename>, start_char=<start_char>)"
        )
    next_steps.append(
        f"  Total found: {total_found} | Currently showing: {first}–{last}"
    )
    lines.append("\n".join(next_steps))

    return "\n".join(lines)
 
@mcp.tool()
async def read_book_content(
    filename: str,
    start_char: int = 0,
    end_char: int = -1,
    max_chars: int = 5000,
) -> str:
    """
    Read a passage from a Gutenberg book.

    Gutenberg license text and boilerplate are automatically stripped —
    you will never receive license content regardless of start_char.

    start_char and end_char are raw-file offsets, exactly as returned by
    gutenberg_prose_search and get_book_stats. The tool handles translation
    to story-content offsets internally.

    Args:
        filename:           Exact filename.
        start_char:         Starting character position in the raw file (default 0).
                            Use offsets directly from gutenberg_prose_search or
                            get_book_stats chapter offsets.
        end_char:           Ending position in the raw file. Default -1 = start + max_chars.
        max_chars:          Maximum characters to return (default 3000).
                            Set to 0 for no limit.

    Returns: The passage with position metadata and a continue hint.
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

    content, strip_offset = _strip_gutenberg(raw)
    total_length = len(content)

    # Clamp: if start_char is inside the header, begin at story start
    raw_start   = max(start_char, strip_offset)
    content_start = raw_start - strip_offset
    content_start = max(0, min(content_start, total_length))

    if content_start >= total_length:
        return (
            f"start_char={start_char:,} is beyond the end of the story "
            f"({total_length:,} story chars, header is {strip_offset:,} chars).\n"
            "Use get_book_stats() to see actual story length and chapter offsets."
        )

    # Resolve end
    if end_char == -1 or end_char <= start_char:
        content_end = content_start + (max_chars if max_chars > 0 else total_length)
    else:
        content_end = end_char - strip_offset
    content_end = min(content_end, total_length)

    actual_length = content_end - content_start

    if max_chars > 0 and actual_length > max_chars:
        return (
            f"Requested passage ({actual_length:,} chars) exceeds max_chars={max_chars:,}.\n"
            "Narrow end_char, or increase max_chars if you deliberately want a longer read.\n"
            f"Story length: {total_length:,} chars.\n"
            f"Tip: get_book_stats('{filename}') shows chapter offsets."
        )

    if content_start >= content_end:
        return f"Invalid range after offset translation: [{content_start:,}, {content_end:,})"

    passage = content[content_start:content_end]

    # Report positions as raw-file offsets so the LLM can pass them back unchanged
    reported_start = content_start + strip_offset
    reported_end   = content_end   + strip_offset

    return "\n".join([
        f"Book: {filename}",
        f"Passage: raw file chars {reported_start}-{reported_end} ({actual_length} chars of story)",
        f"Story length (boilerplate excluded): {total_length} chars",
        f"\n{'=' * 60}\n",
        passage,
        f"\n{'=' * 60}",
        f"End of passage (raw {reported_start}-{reported_end} of {total_length + strip_offset})",
        f"To continue reading: read_book_content(filename='{filename}', start_char={reported_end})",
    ])

@mcp.tool()
async def gutenberg_debug_offsets(book_id: int, start_char: int) -> str:
    """
    Debug tool: Compares text from the search index vs. the raw file.
    Tests multiple offset hypotheses and does a direct substring search
    to find the exact delta between stored and actual offsets.

    Args:
        book_id:    The numeric book_id from gutenberg_prose_search.
        start_char: The start_char offset from that same search result.
    """
    SEQ_LEN  = 60   # chars to use as the search probe
    SHOW_LEN = 200  # chars to show per candidate

    lines = [f"=== Gutenberg Offset Debug: book_id={book_id}, start_char={start_char:,} ===\n"]

    # ── 1. Pull the indexed paragraph body ────────────────────────────────────
    indexed_text = ""
    try:
        conn = _manticore_conn()
        cur  = conn.cursor(_pymysql.cursors.DictCursor)
        cur.execute(
            "SELECT body, start_char FROM gutenberg_paragraphs "
            "WHERE book_id = %s AND start_char = %s LIMIT 1",
            (book_id, start_char),
        )
        row = cur.fetchone()

        # Also grab the paragraph just before and just after for context
        cur.execute(
            "SELECT start_char, body FROM gutenberg_paragraphs "
            "WHERE book_id = %s AND start_char < %s ORDER BY start_char DESC LIMIT 1",
            (book_id, start_char),
        )
        row_prev = cur.fetchone()

        cur.execute(
            "SELECT start_char, body FROM gutenberg_paragraphs "
            "WHERE book_id = %s AND start_char > %s ORDER BY start_char ASC LIMIT 1",
            (book_id, start_char),
        )
        row_next = cur.fetchone()

        conn.close()
        indexed_text = row["body"] if row else ""
    except Exception as e:
        lines.append(f"❌ Index query error: {type(e).__name__}: {e}")
        return "\n".join(lines)

    if not indexed_text:
        # Exact match failed — fall back to the nearest neighbour so the probe
        # search and file-offset analysis can still run and diagnose the drift.
        fallback_row = None
        fallback_label = ""
        if row_next:
            fallback_row   = row_next
            fallback_label = f"next paragraph (start_char={row_next['start_char']:,})"
        elif row_prev:
            fallback_row   = row_prev
            fallback_label = f"previous paragraph (start_char={row_prev['start_char']:,})"

        if fallback_row:
            lines.append(
                f"⚠️  No paragraph at exact start_char={start_char:,} for book_id={book_id}.\n"
                f"   Using {fallback_label} as probe source for offset analysis.\n"
                f"   (This is expected if the offset came from 'To continue reading' or a chapter heading.)\n"
            )
            indexed_text = fallback_row["body"]
            start_char = fallback_row["start_char"]
        else:
            lines.append(
                f"❌ No paragraphs found at all for book_id={book_id}.\n"
                "   The book may not be indexed."
            )
            return "\n".join(lines)

    lines.append(f"[INDEX] Paragraph at start_char={start_char:,}:")
    lines.append(f"  '{indexed_text[:SHOW_LEN]}…'")
    if row_prev:
        lines.append(f"\n[INDEX] Previous paragraph (start_char={row_prev['start_char']:,}):")
        lines.append(f"  '{row_prev['body'][:100]}…'")
    if row_next:
        lines.append(f"\n[INDEX] Next paragraph (start_char={row_next['start_char']:,}):")
        lines.append(f"  '{row_next['body'][:100]}…'")

    # ── 2. Load the raw file ───────────────────────────────────────────────────
    filename = _GUTENBERG_CATALOG.get(book_id)
    if not filename:
        lines.append(f"\n❌ book_id={book_id} not found in loaded catalog.")
        return "\n".join(lines)

    books_dir = os.path.join(os.path.dirname(__file__), "gutenberg", "books", "txt")
    filepath  = os.path.join(books_dir, filename)

    if not os.path.exists(filepath):
        lines.append(f"\n❌ File not found on disk: {filename}")
        return "\n".join(lines)

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        lines.append(f"\n❌ File read error: {type(e).__name__}: {e}")
        return "\n".join(lines)

    raw_len = len(raw)

    # Strip boilerplate and record the strip offset
    stripped_content, strip_offset = _strip_gutenberg(raw)
    stripped_len = len(stripped_content)

    lines.append(f"\n[FILE]  '{filename}'")
    lines.append(f"  Raw file length  : {raw_len:,} chars")
    lines.append(f"  Strip offset     : {strip_offset:,} chars  (header/boilerplate removed from front)")
    lines.append(f"  Stripped length  : {stripped_len:,} chars")

    # ── 3. Test the four plausible offset interpretations ─────────────────────
    lines.append(f"\n[HYPOTHESIS TEST] Reading {SHOW_LEN} chars at four candidate positions:\n")

    def read_at(text: str, pos: int, label: str) -> str:
        if pos < 0 or pos >= len(text):
            return f"  {label}: ❌ out of range (pos={pos:,}, text_len={len(text):,})"
        snippet = text[pos : pos + SHOW_LEN].replace("\n", "↵")
        return f"  {label} (pos={pos:,}):\n    '{snippet}'"

    # H1: start_char is a raw-file offset  → read raw directly
    lines.append(read_at(raw,             start_char,               "H1  raw[start_char]         "))
    # H2: start_char is a stripped offset → translate to raw by adding strip_offset
    lines.append(read_at(raw,             start_char + strip_offset,"H2  raw[start_char+strip]   "))
    # H3: start_char is a raw offset  → read from stripped view (subtract strip_offset)
    lines.append(read_at(stripped_content, start_char - strip_offset,"H3  stripped[start_char-strip]"))
    # H4: start_char is already a stripped offset → read stripped directly
    lines.append(read_at(stripped_content, start_char,               "H4  stripped[start_char]    "))

    # ── 4. Direct substring search — find the truth ───────────────────────────
    probe = indexed_text[:SEQ_LEN].strip()
    # Normalise whitespace in probe (indexers often collapse whitespace)
    probe_norm = _re.sub(r'\s+', ' ', probe)

    lines.append(f"\n[SEARCH] Hunting for first {SEQ_LEN} chars of indexed text in the file…")
    lines.append(f"  Probe (raw)  : '{probe}'")
    lines.append(f"  Probe (norm) : '{probe_norm}'")

    # Search in raw file (exact)
    found_raw_exact = raw.find(probe)
    # Search in raw file (whitespace-normalised)
    raw_norm = _re.sub(r'\s+', ' ', raw)
    found_raw_norm = raw_norm.find(probe_norm)
    # Search in stripped content (exact)
    found_strip_exact = stripped_content.find(probe)
    # Search in stripped content (whitespace-normalised)
    strip_norm = _re.sub(r'\s+', ' ', stripped_content)
    found_strip_norm = strip_norm.find(probe_norm)

    def delta(found: int, expected: int) -> str:
        if found == -1:
            return "NOT FOUND"
        d = found - expected
        sign = "+" if d >= 0 else ""
        return f"found at {found:,}  (delta vs stored: {sign}{d:,})"

    lines.append(f"\n  In raw file   (exact)         : {delta(found_raw_exact,   start_char)}")
    lines.append(f"  In raw file   (ws-normalised) : {delta(found_raw_norm,    start_char)}")
    lines.append(f"  In stripped   (exact)         : {delta(found_strip_exact, start_char)}")
    lines.append(f"  In stripped   (ws-normalised) : {delta(found_strip_norm,  start_char)}")

    # ── 5. Diagnosis ──────────────────────────────────────────────────────────
    lines.append("\n[DIAGNOSIS]")

    if found_raw_exact == start_char:
        lines.append("✅ PERFECT MATCH: start_char is a raw-file offset (exact). No bug.")
    elif found_strip_exact == start_char:
        lines.append(
            "✅ start_char is a STRIPPED-CONTENT offset (exact).\n"
            "   → read_book_content already subtracts strip_offset, so this is correct.\n"
            "   → But the debug tool's f.seek() was reading the RAW file — that's why it looked wrong.\n"
            "   → No real bug; the debug tool's raw seek was misleading you."
        )
    elif found_raw_exact != -1:
        # Exact match in the raw file but at a different position — the clearest signal.
        # Must be checked BEFORE the ws-normalised branches: ws-normalised search will
        # also find the text (just at a different position), so letting it fire first
        # produces a misleading "whitespace-normalised indexer" diagnosis.
        d = found_raw_exact - start_char
        lines.append(
            f"⚠️  Exact text found in RAW file at {found_raw_exact:,} (expected {start_char:,}).\n"
            f"   Delta = {d:+,} chars.\n"
            f"   → The stored start_char is off by exactly {d:+,} chars from the true raw-file position.\n"
            f"   → Most likely cause: newline encoding differences (\\r\\n vs \\n) between the downloaded text\n"
            f"     and how python reads the file later. Fix index_gutenberg.py to strip \\r before indexing.\n"
            f"   → Re-index the affected books after applying the fix."
        )
    elif found_strip_exact != -1:
        d = found_strip_exact - start_char
        lines.append(
            f"⚠️  Exact text found in STRIPPED content at {found_strip_exact:,} (expected {start_char:,}).\n"
            f"   Delta = {d:+,} chars.\n"
            f"   → strip_offset = {strip_offset:,}; the raw-file position would be {found_strip_exact + strip_offset:,}.\n"
            f"   → Check whether the indexer added or subtracted strip_offset when writing start_char."
        )
    elif found_raw_norm != -1:
        d = found_raw_norm - start_char
        lines.append(
            f"⚠️  Text NOT found at an exact position; closest match is in the RAW file after\n"
            f"   whitespace-normalisation at offset {found_raw_norm:,}.\n"
            f"   Delta = {d:+,} chars.\n"
            f"   → The indexer likely stored offsets from a WHITESPACE-NORMALISED version of the raw file.\n"
            f"   → Every offset drifts by roughly this amount; the drift grows with distance from start.\n"
            f"   → Fix: re-index storing offsets from the ORIGINAL raw file, OR adjust read_book_content\n"
            f"     to re-normalise whitespace before seeking."
        )
    elif found_strip_norm != -1:
        d = found_strip_norm - start_char
        lines.append(
            f"⚠️  Text NOT found at an exact position; closest match is in the STRIPPED content after\n"
            f"   whitespace-normalisation at offset {found_strip_norm:,}.\n"
            f"   Delta = {d:+,} chars.\n"
            f"   → The indexer stored offsets from a whitespace-normalised STRIPPED version.\n"
            f"   → read_book_content must (a) strip boilerplate, then (b) normalise whitespace\n"
            f"     before applying start_char, OR the indexer must store raw-file offsets."
        )
    else:
        lines.append(
            "❌ Text NOT FOUND anywhere in the file (exact or ws-normalised).\n"
            "   Possible causes:\n"
            "   • The file on disk has been modified/replaced since indexing.\n"
            "   • The indexer used a different encoding or normalisation pass (e.g., NFC→NFD, ligatures).\n"
            "   • The Gutenberg catalog points to the wrong file for this book_id.\n"
            f"   Catalog filename : {filename}\n"
            f"   First 200 chars of stripped content:\n   '{stripped_content[:200]}'"
        )

    # ── 6. Quick strip_offset sanity check ────────────────────────────────────
    lines.append(f"\n[STRIP SANITY]  First 120 chars after strip_offset ({strip_offset:,}):")
    lines.append(f"  '{raw[strip_offset : strip_offset + 120].replace(chr(10), '↵')}'")
    lines.append(f"\n[STRIP SANITY]  First 120 chars of stripped_content:")
    lines.append(f"  '{stripped_content[:120].replace(chr(10), '↵')}'")
    lines.append("  (These two should be identical — if not, _strip_gutenberg has a bug.)")

    return "\n".join(lines)

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
