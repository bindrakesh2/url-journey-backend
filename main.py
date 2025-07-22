import asyncio
import sys
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

# --- FIX for Windows + Python Compatibility ---
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# --- Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Lifespan Manager for a Single Browser Instance ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup: Launching browser...")
    browser_instance = None
    try:
        p = await async_playwright().start()
        browser_instance = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        app.state.browser = browser_instance
        logger.info("Browser launched successfully.")
        yield
    finally:
        if browser_instance:
            await browser_instance.close()
            logger.info("Browser closed.")
        logger.info("Application shutdown complete.")

# --- FastAPI App Initialization ---
app = FastAPI(lifespan=lifespan)

# --- CORS Middleware ---
origins = [
    "http://localhost:3000",
    "https://urljourney.netlify.app",
    "*"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Advanced Server Name Detection Logic (Your Original, Complete Logic) ---
AKAMAI_IP_RANGES = ["23.192.0.0/11", "104.64.0.0/10", "184.24.0.0/13"]
ip_cache = {}

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

# --- Core URL Analysis Logic using Playwright ---
async def fetch_url_with_playwright(browser: Browser, url: str):
    context = None
    start_time = time.time()
    redirect_chain = []
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
            ignore_https_errors=True
        )
        page = await context.new_page()
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "stylesheet", "font", "media"} else route.continue_())

        async def handle_response(response):
            if response.request.is_navigation_request():
                try:
                    headers = await response.all_headers()
                    hop = {"url": response.url, "status": response.status, "server": get_server_name(headers, response.url), "timestamp": time.time() - start_time}
                    if not redirect_chain or redirect_chain[-1]["url"] != hop["url"]:
                        redirect_chain.append(hop)
                except Exception as e:
                    logger.error(f"Error in handle_response for {url}: {e}")

        page.on("response", handle_response)
        
        # --- MODIFIED: Stricter timeout to act as a safety valve ---
        response = await page.goto(url, timeout=20000, wait_until="domcontentloaded") # 20 seconds
        
        final_url = page.url
        if not redirect_chain or redirect_chain[-1]['url'] != final_url:
             headers = await response.all_headers()
             hop = {"url": final_url, "status": response.status, "server": get_server_name(headers, final_url), "timestamp": time.time() - start_time}
             if not any(h['url'] == hop['url'] and h['status'] == hop['status'] for h in redirect_chain):
                redirect_chain.append(hop)

        return {"originalURL": url, "finalURL": final_url, "redirectChain": redirect_chain, "totalTime": time.time() - start_time}

    except Exception as e:
        error_comment = "An error occurred during navigation"
        if isinstance(e, PlaywrightTimeoutError):
            error_comment = "Navigation timed out after 20s"
        elif "Page crashed" in str(e):
            error_comment = "Navigation failed: Page crashed (out of memory)"
        elif "net::ERR_NAME_NOT_RESOLVED" in str(e):
            error_comment = "Navigation failed: DNS resolution error"
        else:
            error_comment = "Navigation failed: Could not load page"
        
        logger.warning(f"Error for {url}: {error_comment}")
        return {"originalURL": url, "error": error_comment, "redirectChain": redirect_chain}
            
    finally:
        if context:
            await context.close()


@app.websocket("/analyze")
async def analyze_urls_websocket(websocket: WebSocket):
    await websocket.accept()
    try:
        browser = websocket.app.state.browser
        semaphore = asyncio.Semaphore(5)

        async def resilient_fetch(url, ws):
            """This wrapper ensures a single failure doesn't stop the 'gather'."""
            try:
                async with semaphore:
                    result = await fetch_url_with_playwright(browser, url)
                if ws.client_state.name == 'CONNECTED':
                    await ws.send_json(result)
            except Exception as e:
                logger.error(f"A critical, unhandled exception occurred for {url}: {e}", exc_info=True)
                error_result = {"originalURL": url, "error": "A critical processing error occurred"}
                if ws.client_state.name == 'CONNECTED':
                    await ws.send_json(error_result)

        data = await websocket.receive_json()
        raw_urls = data.get("urls", [])
        
        cleaned_urls = [u.strip() for u in raw_urls if u.strip()]
        unique_urls = list(dict.fromkeys(cleaned_urls))

        tasks = [asyncio.create_task(resilient_fetch(url, websocket)) for url in unique_urls]
        
        if tasks:
            await asyncio.gather(*tasks)
        
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_json({"done": True})
            
    except WebSocketDisconnect:
        logger.info("Client disconnected.")
    except Exception as e:
        logger.error(f"General WebSocket error: {e}", exc_info=True)
    finally:
        logger.info("WebSocket connection closed.")

@app.get("/test")
async def test():
    return {"status": "OK", "message": "Service operational"}
