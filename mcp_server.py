import os
import argparse
os.environ["TERM"] = "dumb"
os.environ["NO_COLOR"] = "1"

import asyncio
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup
import httpx
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright
from starlette.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# SCREENSHOT STORAGE
# ---------------------------------------------------------------------------

SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

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
        # Inject extra routes
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

    async def get_browser(self):
        if not self.playwright:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox"]
            )
        return self.browser

browser_manager = BrowserManager()


def parse_html_text(html_content: str) -> str:
    """Extract readable text from HTML."""
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup(["script", "style"]):
        script.extract()
    return soup.get_text(separator="\n", strip=True)


def save_screenshot(data: bytes, prefix: str = "screenshot") -> str:
    """Save screenshot bytes to disk, return the Markdown image tag."""
    filename = f"{prefix}_{int(datetime.now().timestamp())}.png"
    filepath = SCREENSHOT_DIR / filename
    filepath.write_bytes(data)
    
    # The URL that SillyTavern needs to fetch
    url = f"http://localhost:6969/screenshots/{filename}"
    
    # Return as Markdown so the UI renders it
    return f"![{filename}]({url})"


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


@mcp.tool()
async def restart_llama() -> str:
    """Restart the llama server."""
    subprocess.Popen(["pkill", "-9", "llama-server"])
    return "Llama server killed. It should restart shortly if managed by a service."


# ---------------------------------------------------------------------------
# HTTP / WEB TOOLS
# ---------------------------------------------------------------------------

@mcp.tool()
async def http_get_text(url: str, user_agent: str = None, referer: str = None) -> str:
    """Read and extract readable text from a webpage using HTTP GET."""
    headers = {"User-Agent": user_agent or "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        return parse_html_text(resp.text)


@mcp.tool()
async def http_get_image(url: str, user_agent: str = None) -> str:
    """Download an image and display it in chat."""
    headers = {"User-Agent": user_agent or "Mozilla/5.0"}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        mime_type = resp.headers.get("content-type", "image/jpeg")
        ext = mime_type.split("/")[-1].split(";")[0] if "/" in mime_type else "jpeg"
        
        filename = f"image_{int(datetime.now().timestamp())}.{ext}"
        filepath = SCREENSHOT_DIR / filename
        filepath.write_bytes(resp.content)

        public_url = f"http://localhost:6969/screenshots/{filename}"
        return f"![Image]({public_url})"


@mcp.tool()
async def web_search(query: str, page: int = 0, user_agent: str = None) -> str:
    """Search the web using DuckDuckGo lite."""
    headers = {"User-Agent": user_agent or "Mozilla/5.0"}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query, "s": page * 10},
            headers=headers
        )
        return parse_html_text(resp.text)


# ---------------------------------------------------------------------------
# PLAYWRIGHT (HEADLESS BROWSER) TOOLS
# ---------------------------------------------------------------------------

@mcp.tool()
async def puppeteer_screenshot(
    url: str,
    wait_until: str = "networkidle",
    wait_for_selector: str = None
) -> str:
    """Take a full-page screenshot of a webpage. Returns a URL to view the screenshot."""
    browser = await browser_manager.get_browser()
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until=wait_until)
        if wait_for_selector:
            await page.wait_for_selector(wait_for_selector, timeout=15000)
        data = await page.screenshot(full_page=True)
        url_out = save_screenshot(data)
        return f"Screenshot saved. View it at: {url_out}"
    finally:
        await page.close()


@mcp.tool()
async def puppeteer_session_create(url: str, wait_until: str = "networkidle") -> str:
    """Create a persistent browser session. Returns a session_id for future calls."""
    browser = await browser_manager.get_browser()
    page = await browser.new_page()

    browser_manager.counter += 1
    session_id = f"session_{browser_manager.counter}"
    browser_manager.sessions[session_id] = page

    await page.goto(url, wait_until=wait_until)
    return f"Session created. session_id: {session_id}"


@mcp.tool()
async def puppeteer_session_screenshot(session_id: str) -> str:
    """Take a screenshot of a running session. Returns a URL to view the screenshot."""
    page = browser_manager.sessions.get(session_id)
    if not page:
        return "Error: No session found with that session_id."

    data = await page.screenshot(full_page=True)
    url_out = save_screenshot(data, prefix=session_id)
    return f"Screenshot saved. View it at: {url_out}"


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
        return "Error: No session found with that session_id."

    await page.goto(url, wait_until=wait_until)
    if wait_for_selector:
        await page.wait_for_selector(wait_for_selector, timeout=15000)
    return f"Successfully navigated to {url}"


@mcp.tool()
async def puppeteer_session_get_page_text(session_id: str) -> str:
    """Get the current page text from an existing session."""
    page = browser_manager.sessions.get(session_id)
    if not page:
        return "Error: No session found with that session_id."

    content = await page.content()
    return parse_html_text(content)


@mcp.tool()
async def puppeteer_session_close(session_id: str) -> str:
    """Close and destroy a browser session."""
    page = browser_manager.sessions.pop(session_id, None)
    if not page:
        return "Error: No session found with that session_id."
    await page.close()
    return f"Session {session_id} closed successfully."


# ---------------------------------------------------------------------------
# SERVER RUNNER
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Web Tools MCP Server")
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=4242,
        help="Port to run the server on (default: 6969)"
    )
    args = parser.parse_args()

    # Apply the port dynamically
    mcp.settings.port = args.port

    # Also fix the screenshot URLs to use the correct port
    # (patch the save_screenshot function)
    _original_save = save_screenshot
    def save_screenshot(data: bytes, prefix: str = "screenshot") -> str:
        filename = f"{prefix}_{int(datetime.now().timestamp())}.png"
        filepath = SCREENSHOT_DIR / filename
        filepath.write_bytes(data)
        url = f"http://localhost:{args.port}/screenshots/{filename}"
        return f"![{filename}]({url})"

    # --- THE FIX: Disable colors in Uvicorn's default config dict ---
    import uvicorn
    if "default" in uvicorn.config.LOGGING_CONFIG["formatters"]:
        uvicorn.config.LOGGING_CONFIG["formatters"]["default"]["use_colors"] = False
    if "access" in uvicorn.config.LOGGING_CONFIG["formatters"]:
        uvicorn.config.LOGGING_CONFIG["formatters"]["access"]["use_colors"] = False

    print(f"Web Tools MCP Server is running!")
    print(f"Connect your AI client at:  http://localhost:{args.port}/mcp")
    mcp.run(transport="streamable-http")