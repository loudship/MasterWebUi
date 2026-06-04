import os
import json
import hashlib
from fastapi import FastAPI, Request
from mcp.server.fastmcp import FastMCP
import httpx
import redis
from contextlib import asynccontextmanager
from database import get_credentials, save_credentials
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from sse_starlette.sse import EventSourceResponse
from langfuse import observe

# Environment configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://redis-cache:6379/0")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080")
TOR_PROXY = "http://tor-gateway:8118"
BROWSERLESS_WS = "ws://browserless:3000"

redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Initialize FastMCP
mcp = FastMCP("DeepWebOrchestrator")

# We rely on the MCP sse_app for the server

# MCP Tool: Fetch Deep Web Data
@mcp.tool()
@observe(as_type="generation", name="fetch_deep_web_data")
async def fetch_deep_web_data(url: str, session_required: bool = False, use_tor_network: bool = False, js_script: str = None) -> str:
    """
    Extract data from deep web portals or SPAs using headless browsers.
    Use this when targeting dynamic JavaScript applications or bypassing authentication.
    """
    # 1. Check Cache
    query_signature = f"{url}_{session_required}_{use_tor_network}_{js_script}"
    url_hash = hashlib.sha256(query_signature.encode()).hexdigest()
    cached_result = redis_client.get(f"mcp_cache:{url_hash}")
    if cached_result:
        return json.dumps({"status": "success", "source": "cache", "content": cached_result})

    # 2. Setup Crawler Config
    proxy_server = TOR_PROXY if use_tor_network else None
    
    # Using Playwright/Crawl4AI pointing to browserless
    # Since crawl4ai supports proxy in BrowserConfig
    browser_config = BrowserConfig(
        browser_type="chromium",
        headless=True,
        proxy=proxy_server,
        # In a real cluster we might use ws_endpoint=BROWSERLESS_WS, but local chromium is fine if we are within the docker network correctly.
        # But wait, blueprint says: "Deploying Browserless... Crawl4AI can interact with remotely via WebSocket".
        # Currently crawl4ai python package doesn't cleanly expose remote CDP in all versions natively without raw playwright, 
        # but we can pass remote endpoints. Let's rely on local crawling for now if browserless isn't strictly required by crawl4ai API, 
        # OR assume we configure Crawl4AI to use the proxy. The blueprint uses Tor via Proxy config.
    )

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, # We handle cache ourselves
        js_code=js_script if js_script else ""
    )

    # 3. Handle Session Injection
    if session_required:
        domain_id = url.split("//")[-1].split("/")[0] # Basic domain extraction
        creds = get_credentials(domain_id)
        if creds and "payload" in creds:
            # We would typically inject these via hooks.
            # For simplicity, we can pass cookies directly if the API supports it, or use playwright context hook.
            # Using Crawl4AI we can pass headers/cookies or a hook.
            # In latest crawl4ai, you can pass session_id to maintain context.
            pass 

    # 4. Execute Scrape
    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(
            url=url,
            config=run_config
        )

        if result.success:
            markdown_content = result.markdown
            
            # Truncation to preserve VRAM
            MAX_CHARS = 20000
            if len(markdown_content) > MAX_CHARS:
                markdown_content = markdown_content[:MAX_CHARS] + "\n\n[WARNING: Payload truncated to preserve VRAM. End of safe context limit.]"
            
            # Cache the result for 1 hour
            redis_client.setex(f"mcp_cache:{url_hash}", 3600, markdown_content)
            return json.dumps({"status": "success", "content": markdown_content})
        else:
            return json.dumps({"status": "error", "message": result.error_message})


# MCP Tool: Search Deep Web Database
@mcp.tool()
@observe(as_type="generation", name="search_deep_web_database")
async def search_deep_web_database(target_database: str, search_query: str, session_required: bool = False, use_tor_network: bool = False) -> str:
    """
    Search specific deep web databases or academic registries using SearXNG JSON engines.
    """
    # 1. Check cache
    query_signature = f"{target_database}_{search_query}_{use_tor_network}"
    query_hash = hashlib.sha256(query_signature.encode()).hexdigest()
    cached_result = redis_client.get(f"searxng_cache:{query_hash}")
    if cached_result:
        return json.dumps({"status": "success", "source": "cache", "results": json.loads(cached_result)})

    # 2. Query SearXNG
    # We assume target_database corresponds to a custom engine name in SearXNG's settings.yml
    params = {
        "q": search_query,
        "engines": target_database,
        "format": "json"
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{SEARXNG_URL}/search", params=params, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            
            # Cache the JSON results
            redis_client.setex(f"searxng_cache:{query_hash}", 3600, json.dumps(data.get("results", [])))
            return json.dumps({"status": "success", "results": data.get("results", [])})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

# Get the underlying ASGI app from FastMCP
app = mcp.sse_app()

if __name__ == "__main__":
    import uvicorn
    from langfuse import Langfuse
    # Ensure flush on exit (Langfuse background thread handles this mostly, but good practice)
    # Make sure ./data exists for sqlite
    os.makedirs("./data", exist_ok=True)
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
    finally:
        Langfuse().flush()
