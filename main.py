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

# --- Lifespan Manager to handle a single browser instance ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup: Launching browser...")
    browser_instance = None
    try:
        p = await async_playwright().start()
        # Using --disable-dev-shm-usage is crucial for stability in Docker/cloud environments
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

# Add CORS Middleware to allow your React frontend to connect
origins = [
    "http://localhost:3000",
    "https://urljourney.netlify.app",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper Functions (Unchanged) ---
def resolve_ip(url: str) -> str:
    hostname = urlparse(url).hostname
    if not hostname: return None
    try: return socket.gethostbyname(hostname)
    except (socket.gaierror, TypeError): return None

# (Other helper functions like is_akamai_ip and get_server_name can remain as they were in your original code)
def get_server_name(headers: dict, url: str) -> str:
    headers = {k.lower(): v for k, v in headers.items()}
    server_value = headers.get("server", "").lower()
    if server_value:
        if "akamai" in server_value or "ghost" in server_value: return "Akamai"
        if "apache" in server_value: return "Apache (AEM)"
        return server_value.capitalize()
    return "Unknown"


# --- Core Analysis Logic with Robust Error Handling ---
async def fetch_url_with_playwright(browser: Browser, url: str):
    context = None
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ignore_https_errors=True # Be more lenient with SSL issues
        )
        page = await context.new_page()
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
        
        response = await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        
        # Ensure the final response is captured if it wasn't by the event handler
        final_url = page.url
        if not redirect_chain or redirect_chain[-1]["status"] != response.status:
             headers = await response.all_headers()
             hop = {"url": final_url, "status": response.status, "server": get_server_name(headers, final_url), "timestamp": time.time() - start_time}
             redirect_chain.append(hop)

        return {"originalURL": url, "finalURL": final_url, "redirectChain": redirect_chain, "totalTime": time.time() - start_time}

    except Exception as e:
        error_comment = "An error occurred during navigation"
        if isinstance(e, PlaywrightTimeoutError):
            error_comment = "Navigation timed out after 60s"
        elif "net::ERR_NAME_NOT_RESOLVED" in str(e):
            error_comment = "Navigation failed: DNS resolution error"
        else:
            error_comment = "Navigation failed: Could not load page"
        
        logger.warning(f"Error for {url}: {error_comment}")
        # Return an error object that the React frontend can understand
        return {"originalURL": url, "error": error_comment}
            
    finally:
        if context:
            await context.close()


@app.websocket("/analyze")
async def analyze_urls_websocket(websocket: WebSocket):
    await websocket.accept()
    try:
        browser = websocket.app.state.browser
        # A smaller semaphore is better for a resource-intensive task like Playwright
        semaphore = asyncio.Semaphore(5)

        async def resilient_fetch(url, ws):
            """Wrapper to ensure a single failure doesn't crash the whole process."""
            try:
                result = await fetch_url_with_playwright(browser, url)
                if ws.client_state.name == 'CONNECTED':
                    await ws.send_json(result)
            except Exception as e:
                logger.error(f"CRITICAL unhandled exception in fetch for {url}: {e}", exc_info=True)
                error_result = {"originalURL": url, "error": "A critical processing error occurred"}
                if ws.client_state.name == 'CONNECTED':
                    await ws.send_json(error_result)

        data = await websocket.receive_json()
        urls = list(set(filter(None, data.get("urls", []))))
        tasks = []
        for url_str in urls:
            # The React frontend doesn't need "processing" messages, so we remove them
            tasks.append(asyncio.create_task(resilient_fetch(url_str, websocket)))
        
        if tasks:
            await asyncio.gather(*tasks)
        
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_json({"done": True})
            
    except WebSocketDisconnect:
        logger.info("Client disconnected.")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        logger.info("Connection closed.")

@app.get("/test")
async def test():
    return {"status": "OK", "message": "Service operational"}
