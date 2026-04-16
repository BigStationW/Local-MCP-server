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
from bs4 import BeautifulSoup
import httpx
from mcp.server.fastmcp import FastMCP, Image
from playwright.async_api import async_playwright
from starlette.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
import trafilatura
from ddgs import DDGS

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
            http2=True,  # Enable HTTP/2 for better performance
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

def parse_html_text(html_content: str, article_only: bool = False) -> str:
    if article_only:
        result = trafilatura.extract(
            html_content,
            include_comments=False,
            include_tables=True,
            include_links=True,
        )
        if result:
            return result
    # Default: BeautifulSoup full text
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style"]):
        tag.extract()
    return soup.get_text(separator="\n", strip=True) or ""

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
    """Save screenshot bytes to disk, return (public_url_string, Image_object)."""
    filename = f"{prefix}_{int(datetime.now().timestamp())}.png"
    filepath = SCREENSHOT_DIR / filename
    filepath.write_bytes(data)
    url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
    img = Image(data=data, format="png")
    return url, img

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

                filename = f"imgsearch_{int(datetime.now().timestamp())}_{downloaded}.{normalized_fmt}"
                filepath = SCREENSHOT_DIR / filename
                filepath.write_bytes(normalized_data)

                public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
                normalized_data, normalized_fmt = normalize_image_bytes(resp.content, fmt)
                img = Image(data=normalized_data, format=normalized_fmt)

                out.append(f'Result {downloaded + 1}: "{title}" — source: {source}')
                out.append(f"Image URL: {public_url}")
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
            filename = f"pageimg_{session_id}_{int(datetime.now().timestamp())}.{fmt}"
            filepath = SCREENSHOT_DIR / filename
            filepath.write_bytes(resp.content)
            public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
            normalized_data, normalized_fmt = normalize_image_bytes(resp.content, fmt)
            filename = f"pageimg_{session_id}_{int(datetime.now().timestamp())}.{normalized_fmt}"
            filepath = SCREENSHOT_DIR / filename
            filepath.write_bytes(normalized_data)
            img_obj = Image(data=normalized_data, format=normalized_fmt)
            out.append(f"\nLargest image downloaded: {public_url}")
            out.append(img_obj)
        except Exception as e:
            out.append(f"\nCould not auto-download largest image: {e}")

        return out

    except Exception as e:
        return [f"Error extracting images: {str(e)}"]
    
@mcp.tool()
async def web_search_and_read(query: str, max_results: int = 5, read_top_n: int = 2) -> str:
    """Search the web (DuckDuckGo) and then fetch/extract full text from the top N results.
    Use this when snippets aren't enough. May miss content on JavaScript-heavy pages."""
    try:
        items = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                items.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")
                })

        out = []
        out.append("=== Search results ===")
        for i, it in enumerate(items, 1):
            out.append(f"{i}. {it['title']}\n   {it['url']}\n   {it['snippet']}\n")

        out.append("\n=== Full text from top results ===")
        for i, it in enumerate(items[:read_top_n], 1):
            if not it["url"]:
                continue
            out.append(f"\n--- #{i}: {it['url']} ---")
            out.append(await http_get_text(it["url"], article_only=True, max_chars=8000))

        return "\n".join(out)
    except Exception as e:
        return f"Search/read error: {str(e)}"

@mcp.tool()
async def http_get_text(
    url: str,
    user_agent: str = None,
    referer: str = None,
    article_only: bool = True,
    max_chars: int = 8000,
) -> str:
    """Fetch a URL via HTTP GET and extract readable plain text.
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

        filename = f"image_{int(datetime.now().timestamp())}.{fmt}"
        filepath = SCREENSHOT_DIR / filename
        filepath.write_bytes(resp.content)

        public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
        normalized_data, normalized_fmt = normalize_image_bytes(resp.content, fmt)
        filename = f"image_{int(datetime.now().timestamp())}.{normalized_fmt}"
        filepath = SCREENSHOT_DIR / filename
        filepath.write_bytes(normalized_data)
        public_url = f"http://localhost:{SERVER_PORT}/screenshots/{filename}"
        img = Image(data=normalized_data, format=normalized_fmt)
        return [f"Image available at: {public_url}", img]
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
        return [f"Screenshot available at: {public_url}", img]
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
        return [f"Screenshot available at: {public_url}", img]
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

# Swap the default — BeautifulSoup as default, trafilatura as opt-in
@mcp.tool()
async def puppeteer_session_get_page_text(session_id: str, extract_article_only: bool = False) -> str:
    """Get the current page text from an existing session.
    
    Args:
        session_id: The session ID
        extract_article_only: Set to True to extract only main article content (strips nav/menus).
                              Default False returns all page text (better for forums/listings).
    """
    page = browser_manager.sessions.get(session_id)
    if not page:
        return f"Error: No session found with session_id '{session_id}'."
    try:
        content = await page.content()
        return parse_html_text(content, article_only=extract_article_only)
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

# ---------------------------------------------------------------------------
# SERVER RUNNER
# ---------------------------------------------------------------------------
async def shutdown():
    """Cleanup on shutdown."""
    await browser_manager.cleanup()
    await cleanup_http_client()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Web Tools MCP Server")
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=4242,
        help="Port to run the server on (default: 4242)"
    )
    args = parser.parse_args()

    # Set the global port so save_screenshot / http_get_image use it
    SERVER_PORT = args.port
    mcp.settings.port = args.port

    # --- Disable colors in Uvicorn's default config dict ---
    import uvicorn
    if "default" in uvicorn.config.LOGGING_CONFIG["formatters"]:
        uvicorn.config.LOGGING_CONFIG["formatters"]["default"]["use_colors"] = False
    if "access" in uvicorn.config.LOGGING_CONFIG["formatters"]:
        uvicorn.config.LOGGING_CONFIG["formatters"]["access"]["use_colors"] = False

    print(f"Web Tools MCP Server is running!")
    print(f"Connect your AI client at:  http://localhost:{SERVER_PORT}/mcp")
    
    try:
        mcp.run(transport="streamable-http")
    finally:
        # Cleanup on exit
        import asyncio
        asyncio.run(shutdown())
