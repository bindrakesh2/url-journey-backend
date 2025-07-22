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
    "*"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ADVANCED SERVER NAME DETECTION LOGIC (RESTORED) ---
AKAMAI_IP_RANGES = ["23.192.0.0/11", "104.64.0.0/10", "184.24.0.0/13"]
ip_cache = {}

async def resolve_ip_async(hostname: str):
    if not hostname: return None
    if hostname in ip_cache: return ip_cache[hostname]
    try:
        # Run the blocking socket call in a separate thread to avoid freezing the app
        ip = await asyncio.to_thread(socket.gethostbyname, hostname)
        ip_cache[hostname] = ip
        return ip
    except (socket.gaierror, TypeError): return None

def is_akamai_ip(ip: str) -> bool:
    if not ip: return False
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in AKAMAI_IP_RANGES:
            if addr in ipaddress.ip_network(cidr): return True
    except ValueError: pass
    return False

async def get_server_name_advanced(headers: dict, url: str) -> str:
    """This is the fully restored, sophisticated server name detection function."""
    headers = {k.lower(): v for k, v in headers.items()}
    hostname = urlparse(url).hostname

    # Rule 1: Check server header first
    server_value = headers.get("server", "").lower()
    if server_value:
        if "akamai" in server_value or "ghost" in server_value: return "Akamai"
        if "apache" in server_value: return "Apache (AEM)"
        return server_value.capitalize()
    
    # Rule 2: Check server-timing and other Akamai-specific headers
    server_timing = headers.get("server-timing", "")
    has_akamai_cache = "cdn-cache; desc=HIT" in server_timing or "cdn-cache; desc=MISS" in server_timing
    has_akamai_request_id = "x-akamai-request-id" in headers
    
    # Rule 3: Check for AEM-specific headers
    has_dispatcher = "x-dispatcher" in headers or "x-aem-instance" in headers
    has_aem_paths = any("/etc.clientlibs" in v for h, v in headers.items() if h in ["link", "baqend-tags"])
    
    # Rule 4: Resolve IP and check if it's in Akamai's known ranges
    ip = await resolve_ip_async(hostname)
    is_akamai = is_akamai_ip(ip)

    # Rule 5: Apply logic based on findings
    if has_akamai_cache or has_akamai_request_id or (server_timing and is_akamai):
        if has_aem_paths or has_dispatcher: return "Apache (AEM)"
        return "Akamai"
    
    if has_dispatcher or has_aem_paths: return "Apache (AEM)"
    if is_akamai: return "Akamai"
    
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
            server_name = await get_server_name_advanced(response.headers, str(response.url))
            
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
                
                current_url = urljoin(str(response.url), target_url)
            else:
                response.raise_for_status()
                break
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
        
        CONCURRENCY_LIMIT = 100
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def bound_check(url, client):
            async with semaphore:
                return await check_url_status(client, url)

        async with httpx.AsyncClient(http2=True, limits=httpx.Limits(max_connections=200)) as client:
            tasks = []
            for url_str in urls:
                if url_str.strip():
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
