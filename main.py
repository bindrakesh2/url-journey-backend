import asyncio
import httpx
import uvicorn
import logging
import socket
import ipaddress
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from urllib.parse import urlparse, urljoin
import time

# --- Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- CORS Middleware ---
origins = [
    "http://localhost:3000",
    "https://urljourney.netlify.app",
    "*" # Using a wildcard for flexibility
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Helper Functions ---
def get_server_name(headers: dict) -> str:
    server_value = headers.get("server", "").lower()
    if server_value:
        if "akamai" in server_value or "ghost" in server_value: return "Akamai"
        if "apache" in server_value: return "Apache (AEM)"
        return server_value.capitalize()
    return "Unknown"

# --- Core URL Analysis Logic (httpx version) ---
async def check_url_status(client: httpx.AsyncClient, url: str):
    start_time = time.time()
    redirect_chain = []
    current_url = url
    MAX_REDIRECTS = 15

    try:
        for _ in range(MAX_REDIRECTS):
            response = await client.get(current_url, follow_redirects=False, timeout=30.0)
            server_name = get_server_name(response.headers)
            
            hop_info = {
                "url": str(response.url), 
                "status": response.status_code, 
                "server": server_name,
                "timestamp": time.time() - start_time
            }
            redirect_chain.append(hop_info)
            
            if response.is_redirect:
                target_url = response.headers.get('location')
                if not target_url:
                    raise Exception("Redirect missing location header")
                
                # Resolve relative URLs to absolute URLs
                current_url = urljoin(str(response.url), target_url)
            else:
                response.raise_for_status()
                break # Exit loop on a final, non-redirect status
        else:
            raise Exception("Too many redirects")

        return {
            "originalURL": url, 
            "finalURL": current_url,
            "redirectChain": redirect_chain,
            "totalTime": time.time() - start_time
        }

    except Exception as e:
        error_message = "An error occurred"
        if isinstance(e, httpx.HTTPStatusError):
            error_message = f"HTTP Error: {e.response.status_code}"
        elif isinstance(e, httpx.RequestError):
            error_message = "Request failed (e.g., DNS or network error)"
        else:
            error_message = str(e)

        # Ensure the final object for the frontend is consistent
        return {
            "originalURL": url,
            "finalURL": current_url if current_url != url else None,
            "redirectChain": redirect_chain,
            "totalTime": time.time() - start_time,
            "error": error_message
        }

@app.websocket("/analyze")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        urls = data.get("urls", [])
        
        CONCURRENCY_LIMIT = 100 # We can use higher concurrency with httpx
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def bound_check(url, client):
            async with semaphore:
                return await check_url_status(client, url)

        async with httpx.AsyncClient(http2=True, limits=httpx.Limits(max_connections=200)) as client:
            tasks = []
            for url_str in urls:
                if url_str.strip(): # Process only non-empty URLs
                    tasks.append(asyncio.create_task(bound_check(url_str.strip(), client)))

            for future in asyncio.as_completed(tasks):
                result = await future
                if websocket.client_state.name == 'CONNECTED':
                    await websocket.send_json(result)
        
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
