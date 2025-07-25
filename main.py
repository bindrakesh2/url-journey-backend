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

# --- Browser User-Agent ---
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

# --- Advanced Server Name Detection Logic (Unchanged) ---
AKAMAI_IP_RANGES = ["23.192.0.0/11", "104.64.0.0/10", "184.24.0.0/13"]
ip_cache = {}

async def resolve_ip_async(hostname: str):
    if not hostname: return None
    if hostname in ip_cache: return ip_cache[hostname]
    try:
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
    has_dispatcher = "x-dispatcher" in headers or "x-aem-instance" in headers
    has_aem_paths = any("/etc.clientlibs" in v for h, v in headers.items() if h in ["link", "baqend-tags"])
    
    ip = await resolve_ip_async(hostname)
    is_akamai = is_akamai_ip(ip)

    if has_akamai_cache or has_akamai_request_id or (server_timing and is_akamai):
        if has_aem_paths or has_dispatcher: return "Apache (AEM)"
        return "Akamai"
    
    if has_dispatcher or has_aem_paths: return "Apache (AEM)"
    if is_akamai: return "Akamai"
    
    return "Unknown"

# --- Core URL Analysis Logic (Unchanged) ---
async def check_url_status(client: httpx.AsyncClient, url: str):
    start_time = time.time()
    redirect_chain = []
    current_url = url
    error_message = None
    MAX_REDIRECTS = 15

    try:
        for _ in range(MAX_REDIRECTS):
            try:
                response = await client.get(current_url, follow_redirects=False, timeout=60.0)
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
                    
                    next_url = urljoin(str(response.url), target_url)
                    if next_url == current_url:
                        raise Exception("URL redirects to itself in a loop")
                    
                    current_url = next_url
                else:
                    response.raise_for_status()
                    break 
            
            except httpx.HTTPStatusError as e:
                break

        if len(redirect_chain) >= MAX_REDIRECTS:
             error_message = "Too many redirects"

    except Exception as e:
        if isinstance(e, httpx.TimeoutException):
            error_message = "Request timed out after 60s"
        elif isinstance(e, httpx.RequestError):
            error_message = "Request failed (Network/DNS error)"
        else:
            error_message = str(e)

    final_result = {
        "originalURL": url,
        "finalURL": redirect_chain[-1]['url'] if redirect_chain else url,
        "redirectChain": redirect_chain,
        "totalTime": time.time() - start_time,
    }
    if error_message:
        final_result["error"] = error_message

    return final_result

@app.websocket("/analyze")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        urls = data.get("urls", [])
        
        # --- MODIFIED: Reduced concurrency to avoid rate limiting ---
        CONCURRENCY_LIMIT = 20
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def bound_check(url, client):
            async with semaphore:
                return await check_url_status(client, url)

        async with httpx.AsyncClient(http2=True, limits=httpx.Limits(max_connections=200), verify=False, headers=BROWSER_HEADERS) as client:
            tasks = []
            for url_str in urls:
                url = url_str.strip()
                if url:
                    if not url.startswith(('http://', 'https://')):
                        url = f'https://{url}'
                    tasks.append(asyncio.create_task(bound_check(url, client)))

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
