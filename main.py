# main.py
import asyncio
import sys

# --- FIX for Windows + Python Compatibility ---
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# --- End of Fix ---

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, Browser, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
import logging
import validators
from typing import List, Dict, Any
import time
import json
import os
import socket
import ipaddress
from urllib.parse import urlparse
from contextlib import asynccontextmanager
import uvicorn

# --- Custom Exception ---
class TooManyRedirectsError(Exception):
    pass

# --- Lifespan Manager with Robust Shutdown ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handles startup and shutdown to manage a single browser instance."""
    logger.info("Application startup: Launching browser...")
    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            app.state.browser = browser
            logger.info("Browser launched successfully and is ready for requests.")
            yield
        finally:
            if browser:
                # --- SHUTDOWN FIX 1: Add a timeout to browser.close() ---
                try:
                    logger.info("Attempting to gracefully close the browser...")
                    await asyncio.wait_for(browser.close(), timeout=5.0)
                    logger.info("Browser closed successfully.")
                except asyncio.TimeoutError:
                    logger.warning("Browser did not close within 5 seconds. The process will now exit.")
                # --- End of Fix ---
            logger.info("Application shutdown complete.")

# --- FastAPI App Initialization ---
app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["https://urljourney.netlify.app/", "*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration & Caches ---
AKAMAI_IP_RANGES = ["23.192.0.0/11", "104.64.0.0/10", "184.24.0.0/13"]
ip_cache = {}

# --- Helper Functions (Unchanged) ---
def resolve_ip(url: str) -> str:
    hostname = urlparse(url).hostname
    if not hostname: return None
    if hostname in ip_cache: return ip_cache[hostname]
    try:
        ip = socket.gethostbyname(hostname)
        ip_cache[hostname] = ip
        return ip
    except (socket.gaierror, TypeError):
        return None
def is_akamai_ip(ip: str) -> bool:
    if not ip: return False
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in AKAMAI_IP_RANGES:
            if addr in ipaddress.ip_network(cidr):
                return True
    except ValueError:
        pass
    return False
def get_server_name(headers: dict, url: str) -> str:
    headers = {k.lower(): v for k, v in headers.items()}
    hostname = urlparse(url).hostname
    if hostname and ("bmw" in hostname.lower() or "mini" in hostname.lower()):
        if "cache-control" in headers:
            return "Apache (AEM)"
    server_value = headers.get("server", "").lower()
    if server_value:
        if "akamai" in server_value or "ghost" in server_value: return "Akamai"
        if "apache" in server_value: return "Apache (AEM)"
        return server_value.capitalize()
    server_timing = headers.get("server-timing", "")
    has_akamai_cache = "cdn-cache; desc=HIT" in server_timing or "cdn-cache; desc=MISS" in server_timing
    has_akamai_request_id = "x-akamai-request-id" in headers
    ip = resolve_ip(url)
    is_akamai = is_akamai_ip(ip)
    has_dispatcher = "x-dispatcher" in headers or "x-aem-instance" in headers
    has_aem_paths = any("/etc.clientlibs" in v for h, v in headers.items() if h in ["link", "baqend-tags"])
    if has_akamai_cache or has_akamai_request_id or (server_timing and is_akamai):
        if has_aem_paths or has_dispatcher: return "Apache (AEM)"
        return "Akamai"
    if has_dispatcher or has_aem_paths: return "Apache (AEM)"
    if is_akamai: return "Akamai"
    return "Unknown"

# --- Core Analysis Logic ---
async def fetch_url_with_playwright(browser: Browser, url: str, websocket: WebSocket):
    context = None
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        # --- SHUTDOWN FIX 2: Add a page error listener for stability ---
        page.on("pageerror", lambda exc: logger.error(f"Page Error on {url}: {exc}"))
        # --- End of Fix ---

        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "stylesheet", "font", "media"} else route.continue_())
        
        redirect_chain = []
        start_time = time.time()

        async def handle_response(response):
            if response.request.is_navigation_request():
                try:
                    headers = await response.all_headers()
                    hop = {"url": response.url, "status": response.status, "server": get_server_name(headers, response.url), "timestamp": time.time() - start_time}
                    if not redirect_chain or redirect_chain[-1]["url"] != hop["url"]:
                        redirect_chain.append(hop)
                except Exception as e:
                    logger.error(f"Error in handle_response for {response.url}: {e}")

        page.on("response", handle_response)
        
        try:
            response = await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        except PlaywrightError as e:
            if "net::ERR_TOO_MANY_REDIRECTS" in str(e):
                raise TooManyRedirectsError("Browser detected too many redirects.")
            else:
                raise e # Re-raise other Playwright errors (like timeouts)

        final_url = page.url
        if not redirect_chain or redirect_chain[-1]["url"] != final_url:
            final_headers = await response.all_headers() if response else {}
            hop = {"url": final_url, "status": response.status if response else 200, "server": get_server_name(final_headers, final_url), "timestamp": time.time() - start_time}
            redirect_chain.append(hop)
        
        result = {"originalURL": url, "finalURL": final_url, "redirectChain": redirect_chain, "totalTime": time.time() - start_time}
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_text(json.dumps(result))

    except TooManyRedirectsError as e:
        logger.warning(f"Caught 'Too many redirects' for {url}")
        result = {"originalURL": url, "finalURL": url, "redirectChain": [], "error": str(e)}
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_text(json.dumps(result))
    except PlaywrightTimeoutError:
        error_message = "Navigation timed out after 60s (may be a slow page)"
        logger.warning(f"Timeout error for {url}")
        result = {"originalURL": url, "finalURL": url, "redirectChain": [], "error": error_message}
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_text(json.dumps(result))
    except asyncio.CancelledError:
        logger.info(f"Task for {url} was cancelled because client disconnected.")
    except Exception as e:
        logger.error(f"Critical error fetching {url}: {e}", exc_info=True)
        result = {"originalURL": url, "finalURL": None, "redirectChain": [], "error": "A critical server error occurred."}
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_text(json.dumps(result))
    finally:
        if context:
            await context.close()


# (The rest of the file is unchanged)
async def validate_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    if not validators.url(url):
        raise ValueError(f"Invalid URL: {url}")
    return url

@app.websocket("/analyze")
async def analyze_urls_websocket(websocket: WebSocket):
    await websocket.accept()
    browser = websocket.app.state.browser
    semaphore = asyncio.Semaphore(1)

    async def limited_fetch(url):
        async with semaphore:
            await fetch_url_with_playwright(browser, url, websocket)

    active_tasks = []
    try:
        data = await websocket.receive_json()
        urls = list(set(filter(None, data.get("urls", []))))
        validated_urls = []
        for url in urls:
            try:
                validated_urls.append(await validate_url(url))
            except ValueError:
                await websocket.send_text(json.dumps({"error": f"Invalid URL: {url}", "originalURL": url}))
        
        valid_urls_only = [u for u in validated_urls if u]
        for url in valid_urls_only:
            await websocket.send_text(json.dumps({"status": "processing", "url": url}))
        
        for url in valid_urls_only:
            task = asyncio.create_task(limited_fetch(url))
            active_tasks.append(task)
        
        await asyncio.gather(*active_tasks, return_exceptions=True)
        
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_text(json.dumps({"done": True}))
    except WebSocketDisconnect:
        logger.info("Client disconnected. Cancelling all outstanding analysis tasks.")
        for task in active_tasks:
            task.cancel()
        await asyncio.gather(*active_tasks, return_exceptions=True)
        logger.info("All tasks cancelled.")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)

@app.get("/test")
async def test():
    return {"status": "OK", "message": "Service operational"}

# This file should be run with the `uvicorn` command


