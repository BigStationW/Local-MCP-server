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
async def gutenberg_search(
    query: str,
    language: str = None,
    max_results: int = 10,
    offset: int = 0,
    proximity: int = 50,
    author: str = None,
    category: str = None,
    ranker: str = "proximity_bm25",
) -> str:
    """
    Search the full prose text of all indexed Gutenberg books by concrete word clusters.
    Returns highlighted paragraphs with book IDs, and start_char offsets
    ready to pass directly to read_book_content.

    Results are ranked according to the `ranker` option. The default (proximity_bm25)
    combines BM25 term-frequency relevance with word-proximity scoring and weights
    body text 10× higher than title text.

    Args:
        query:       Manticore full-text query. Supports operators:
                       - AND (default): words separated by space must all appear
                             e.g. "propriety decorum silence"
                       - OR:  use | between terms or phrases
                             e.g. "vanity | pride"
                       - Exact phrase: wrap in double quotes
                             e.g. '"play a part"'
                       - OR between phrases: '"play a part" | "play the comedy"'
                       - NOT: prefix a word with - to exclude it
                             e.g. "love -marriage"
                       - Mix freely: '"play a part" | vanity decorum -comedy'
                       Do NOT use SQL-style OR/AND keywords — use | and spaces instead.
                       Start with 2-4 words; simplify if no results appear.
        language:    Two-letter code (e.g. "en", "la"). Default None (all languages).
        max_results: Number of paragraphs to return per page (default 10).
        offset:      Zero-based index of the first result to return (default 0).
                     Use offset=10 to get results 11-20, offset=20 for 21-30, etc.
        proximity:   Max token distance between query words for the NEAR fallback (default 50).
                     Only applies when the raw query returns 0 results and Manticore
                     retries with a NEAR/N proximity query built from plain words.
                     Increase to 100-200 if the fallback also returns nothing.
        author:      Optional. Filter by author name (case-insensitive partial match).
                     e.g. "Dickens" or "Carroll".
        category:    Optional. Filter by bookshelf category (case-insensitive partial match).
                     e.g. "Historical Fiction" or "Philosophy".
        ranker:      Ranking algorithm. Options:
                       "proximity_bm25" (default) — best for targeted searches; rewards
                           rare words appearing frequently and close together.
                       "bm25"           — pure term-frequency relevance, ignores proximity;
                           good for broad thematic searches where word order doesn't matter.
                       "sph04"          — like proximity_bm25 but also rewards exact phrase
                           matches; best when your query is a known exact phrase or title.
                       "wordcount"      — ranks by raw count of matched query words in the
                           paragraph; good for finding dense passages on a topic.
                       "none"           — no ranking (fastest); returns results in index order,
                           giving a natural cross-section of passage types — best for
                           stylistic exploration and finding inspiration.

    Workflow:
        gutenberg_search(query="...")                          ← first page
        gutenberg_search(query="...", offset=10)              ← next page
        read_book_content(book_id=..., start_char=...)
    """
    VALID_RANKERS = {"proximity_bm25", "bm25", "sph04", "wordcount", "none"}
    if ranker not in VALID_RANKERS:
        return (
            f"Invalid ranker '{ranker}'. Choose from: {', '.join(sorted(VALID_RANKERS))}.\n"
            "Default is 'proximity_bm25'."
        )

    # Extract plain words (no operators) for the NEAR/fallback path only.
    plain_words = [w.strip() for w in re.sub(r'[^\w\s]', '', query).split() if w.strip()]
    if not plain_words:
        return "Empty query."

    def _escape_fts(q: str) -> str:
        """Escape single quotes for SQL safety. Preserve all FTS operators."""
        return q.replace("'", "''")

    def _make_sql(fts_expr: str, use_ranker: str) -> str:
        # When author/category filters are present, wrap the body expression in parens
        # so the field anchors (@author, @bookshelves) apply at the right level.
        body_part = f"({fts_expr})" if author or category else fts_expr
        author_part = f" @author {_escape_fts(author)}" if author else ""
        category_part = f" @bookshelves {_escape_fts(category)}" if category else ""
        full_match = f"@body {body_part}{author_part}{category_part}"
        fts_safe = _escape_fts(full_match)
        lang_filter = f" AND language='{_escape_fts(language[:5])}'" if language else ""

        highlight_opts = "before_match='**', after_match='**', limit=500, around=20"

        return (
            f"SELECT book_id, title, author, language, bookshelves, start_char, body, "
            f"HIGHLIGHT({{{highlight_opts}}}, 'body') AS snippet "
            f"FROM gutenberg_paragraphs "
            f"WHERE MATCH('{fts_safe}'){lang_filter} "
            f"LIMIT {int(offset)}, {int(max_results)} "
            f"OPTION ranker={use_ranker}, field_weights=(body=10,title=1)"
        )

    raw_fts       = _escape_fts(query)
    proximity_fts = (
        plain_words[0] if len(plain_words) == 1
        else f" NEAR/{int(proximity)} ".join(plain_words)
    )
    fallback_fts  = " ".join(plain_words)

    used_fallback  = False
    fallback_label = ""

    try:
        conn = _manticore_conn()
        cur = conn.cursor(_pymysql.cursors.DictCursor)

        # Tier 1: raw query (preserves |, "", -, etc.)
        cur.execute(_make_sql(raw_fts, ranker))
        rows = cur.fetchall()
        cur.execute("SHOW META")
        meta = {r["Variable_name"]: r["Value"] for r in cur.fetchall()}
        total_found = int(meta.get("total_found", len(rows)))

        # Tier 2: NEAR/N on plain words (only when raw returned nothing and
        #          there are multiple words to constrain)
        if not rows and len(plain_words) > 1 and proximity_fts != raw_fts:
            cur.execute(_make_sql(proximity_fts, ranker))
            rows = cur.fetchall()
            cur.execute("SHOW META")
            meta = {r["Variable_name"]: r["Value"] for r in cur.fetchall()}
            total_found = int(meta.get("total_found", len(rows)))
            if rows:
                used_fallback  = True
                fallback_label = f"NEAR/{proximity} on plain words"

        # Tier 3: plain AND (broadest — last resort)
        if not rows and fallback_fts != proximity_fts:
            cur.execute(_make_sql(fallback_fts, ranker))
            rows = cur.fetchall()
            cur.execute("SHOW META")
            meta = {r["Variable_name"]: r["Value"] for r in cur.fetchall()}
            total_found = int(meta.get("total_found", len(rows)))
            if rows:
                used_fallback  = True
                fallback_label = "plain AND (words may be far apart)"

        conn.close()

    except _pymysql.OperationalError as e:
        return (
            f"❌ Cannot connect to Manticore Search: {e}\n"
            "Fix: Win+R → services.msc → ManticoreSearch → Start"
        )
    except Exception as e:
        return f"Manticore query error: {type(e).__name__}: {e}"

    lang_display = language or "any"

    if not rows:
        filter_note = ""
        if author or category:
            parts = []
            if author:   parts.append(f"author='{author}'")
            if category: parts.append(f"category='{category}'")
            filter_note = f" with filters ({', '.join(parts)})"
        return (
            f"No prose matches for '{query}'{filter_note} "
            f"(language={lang_display}, proximity={proximity}).\n\n"
            "Diagnosis:\n"
            "  1. Use | for OR, not the word OR: 'vanity | pride'\n"
            "  2. Wrap exact phrases in double quotes: '\"play a part\"'\n"
            "  3. Try fewer words — 2 is often better than 4.\n"
            "  4. Increase proximity= to 150 or 200.\n"
            "  5. Use words from the middle of sentences, not headings or dialogue tags.\n"
            "  6. To restrict language, pass language='en' (or another code).\n"
            "  7. If using author= or category=, try broadening or removing those filters."
        )

    fallback_note = (
        f"\n⚠️  Raw query returned 0 results — fell back to {fallback_label}. "
        f"The {len(rows)} results below matched on plain words. "
        f"To use FTS operators, check the query syntax in the docstring.\n"
        if used_fallback else ""
    )

    first = offset + 1
    last  = offset + len(rows)
    lines = [
        f"Found {total_found} prose match(es) for '{query}' "
        f"(language={lang_display}, proximity={proximity}, ranker={ranker}) — displaying {first}–{last}:"
        f"{fallback_note}\n"
    ]

    for i, row in enumerate(rows, 1):
        snippet = (row.get("snippet") or "").strip()
        body = row.get("body") or ""
        para_start = row['start_char']

        # HIGHLIGHT() may return empty for very short paragraphs; degrade gracefully
        if not snippet:
            body_text = (row.get("body") or "").strip()
            if len(body_text) <= 500:
                snippet = f"[{para_start}] {body_text}"
            else:
                m = re.search(r'[.!?]', body_text[500:])
                end_pos = 500 + m.start() + 1 if m else 500
                snippet = f"[{para_start}] {body_text[:end_pos].rsplit(' ', 1)[0]}… [{para_start + end_pos}]"
        else:
            # Replace ... with character positions
            fragments = re.split(r'\s*\.\.\.\s*', snippet)
            result_parts = []
            search_pos = 0
            is_first_fragment = True  # Track actual first fragment
            
            for frag in fragments:
                # Remove ** markers to find position in original body
                clean_frag = frag.replace('**', '').strip()
                if not clean_frag:
                    continue
                    
                # Find fragment in body text
                pos = body.find(clean_frag[:50], search_pos)  # Use first 50 chars for matching
                if pos >= 0:
                    abs_pos = para_start + pos
                    # For first fragment, use paragraph start; for rest, use actual position
                    display_pos = para_start if is_first_fragment else abs_pos
                    result_parts.append(f"[{display_pos}] {frag}")
                    search_pos = pos + len(clean_frag)
                    is_first_fragment = False  # No longer first after this
                else:
                    # Fallback if can't find position
                    display_pos = para_start if is_first_fragment else "?"
                    result_parts.append(f"[{display_pos}] {frag}")
                    is_first_fragment = False
            
            snippet = " ".join(result_parts)

        # 1. Split into lines, strip leading/trailing spaces from each, drop empty lines
        clean_lines = [line.strip() for line in snippet.splitlines() if line.strip()]
        # 2. Join them back with a newline and exactly 3 spaces
        snippet = "\n   ".join(clean_lines)

        bookshelves = (row.get('bookshelves') or '').strip()
        cat_str = f"   categories: {bookshelves}\n" if bookshelves else ""
        lines.append(
            f"{i}. {row['title']} by {row['author']}\n"
            f"   book_id: {row['book_id']} | start_char: {row['start_char']}\n"
            f"{cat_str}"
            f"   Match: {snippet}\n\n"
        )

    next_offset = offset + len(rows)
    prev_offset = max(0, offset - max_results)

    next_steps = ["\nNext steps:"]
    if next_offset < total_found:
        next_steps.append(
            f"  • Continue  → gutenberg_search(query='{query}', offset={next_offset}, "
            f"max_results={max_results}, ranker='{ranker}')"
        )
    if offset > 0:
        next_steps.append(
            f"  • Go back   → gutenberg_search(query='{query}', offset={prev_offset}, "
            f"max_results={max_results}, ranker='{ranker}')"
        )
    next_steps.append(
        "  • Read text → read_book_content(book_id=<book_id>, start_char=<start_char>)"
    )
    next_steps.append(
        f"  Total found: {total_found} | Currently showing: {first}–{last}"
    )
    lines.append("\n".join(next_steps))

    return "\n\u00A0\n".join(lines)
 
@mcp.tool()
async def read_book_content(
    book_id: int,
    start_char: int = 0,
    max_chars: int = 5000,
) -> str:
    """
    Read a passage from a Gutenberg book using the local index.

    Args:
        book_id:    The numeric book_id from gutenberg_search.
        start_char: Starting character position as returned by gutenberg_search (default 0).
        max_chars:  Maximum characters to return (default 5000). Set to 0 for no limit.
    """

    try:
        conn = _manticore_conn()
        cur = conn.cursor(_pymysql.cursors.DictCursor)
        cur.execute(
            "SELECT title, author, language, body, start_char FROM gutenberg_paragraphs "
            "WHERE book_id = %s AND start_char >= %s "
            "ORDER BY start_char ASC "
            "LIMIT 50",
            (book_id, start_char)
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return f"Database error: {type(e).__name__}: {e}"

    if not rows:
        return f"No content found for book_id={book_id} at start_char={start_char}."

    passage = ""
    next_start = rows[-1]["start_char"] + len(rows[-1]["body"])
    for row in rows:
        chunk = row["body"]
        if max_chars > 0 and len(passage) + len(chunk) > max_chars:
            passage += chunk[:max_chars - len(passage)]
            next_start = row["start_char"] + len(chunk)
            break
        passage += chunk + "\n\n"
        next_start = row["start_char"] + len(chunk)

    title = rows[0].get("title", "Unknown")
    author = rows[0].get("author", "Unknown")
    lang = rows[0].get("language", "Unknown")
    return "\n".join([
        f"Book: {title} by {author} (book_id={book_id}, language={lang})",
        f"Passage starting at char {start_char} ({len(passage.strip())} chars returned)",
        f"\n{'=' * 60}\n",
        passage.strip(),
        f"\n{'=' * 60}",
        f"To continue reading: read_book_content(book_id={book_id}, start_char={next_start})",
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
