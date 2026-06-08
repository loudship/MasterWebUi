"""
server.py — Deep Web MCP Server (v3 — Crawl4AI Edition)
=========================================================

Architecture
------------
  FastMCP  ──SSE──►  /sse   (MCP tool discovery + invocation)
  FastAPI  ──POST──► /extract/stream  (raw SSE progress stream)
  FastAPI  ──GET───► /extract/status/{task_id}  (polling fallback)
  FastAPI  ──POST──► /credentials/store

Core tool: fetch_deep_web_data
--------------------------------
Wraps Crawl4AI's AsyncWebCrawler to perform authenticated, anonymised
deep-web extraction with real-time status streaming.

Execution sequence
------------------
1. Cache probe (Redis SHA-256 keyed).
2. If session_required=True: query SQLite CredentialVault by thread_id
   to pull stored JWTs + persistent cookies.
3. Build Crawl4AI BrowserConfig:
   - Inject Privoxy HTTP proxy (http://tor-gateway:8118) when use_tor_network=True.
   - Pass cookies and JWT headers from the vault as browser_kwargs so they are
     injected into the Chromium context via the on_page_context_created hook.
4. Run AsyncWebCrawler.arun() with default markdown conversion (fit_markdown).
5. Process extracted markdown through TextSanitizer pipeline:
   - Strip base64 blobs.
   - Strip nested table blocks.
   - Enforce MAX_CHARS = 20 000 with TRUNCATION_SENTINEL.
6. Cache sanitized output for 3 600 s, return structured JSON.

Error contract
--------------
Any runtime error from Crawl4AI (proxy dropout, navigation timeout,
container crash, shared memory exhaustion) is caught, the browser context
is closed to release shared memory, and a standardised JSON error block is
returned with error_code=EGRESS_TIMEOUT_BREACH.

SSE progress frames (on /extract/stream)
-----------------------------------------
  event: progress  data: {"task_id": str, "progress": 0-100, "status": str}
  event: result    data: {"task_id": str, "content": str}
  event: error     data: {"task_id": str, "error_code": str, "reason": str}

Changes from v2
---------------
- Primary extraction backend changed from PlaywrightExtractor to Crawl4AI
  AsyncWebCrawler.
- Tor proxy switched from SOCKS5 direct to Privoxy HTTP bridge
  (http://tor-gateway:8118) as specified.
- session_required parameter gates credential vault lookup explicitly.
- /sse route added for MCP SSE transport (Starlette SseServerTransport).
- Pydantic v2-compatible field declarations.
- Existing endpoints (/extract/stream, /extract/status, /credentials/store)
  preserved and updated to use the new extraction path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import redis
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

# Starlette SSE transport (MCP /sse route)
from mcp.server.sse import SseServerTransport

# SSE event-source for streaming endpoints
from sse_starlette.sse import EventSourceResponse

# Local modules
from database import get_credentials, save_credentials
from sanitizer import TextSanitizer, MAX_CHARS, TRUNCATION_SENTINEL

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

REDIS_URL:       str = os.getenv("REDIS_URL",       "redis://redis-cache:6379/0")
SEARXNG_URL:     str = os.getenv("SEARXNG_URL",     "http://searxng:8080")

# Privoxy HTTP→SOCKS5 bridge for Tor traffic
TOR_HTTP_PROXY:  str = os.getenv("TOR_HTTP_PROXY",  "http://tor-gateway:8118")

# Page navigation timeout (ms)
PAGE_TIMEOUT_MS: int = int(os.getenv("PAGE_TIMEOUT_MS", "45000"))

# Redis connection
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Singleton sanitizer
_sanitizer = TextSanitizer(max_chars=MAX_CHARS)


# ---------------------------------------------------------------------------
# Task progress registry (shared with SSE stream generator)
# ---------------------------------------------------------------------------

@dataclass
class ExtractionTask:
    task_id:    str
    url:        str
    progress:   int   = 0
    status:     str   = "pending"   # pending | running | done | error
    result:     Optional[dict] = None
    started_at: float = field(default_factory=time.time)

_task_registry: dict[str, ExtractionTask] = {}


def _get_task(task_id: str) -> Optional[ExtractionTask]:
    return _task_registry.get(task_id)


def _set_task_progress(task_id: str, progress: int, status: str = "running") -> None:
    task = _task_registry.get(task_id)
    if task:
        task.progress = progress
        task.status   = status


# ---------------------------------------------------------------------------
# Standardised JSON error block
# ---------------------------------------------------------------------------

def _error_block(
    task_id:    str,
    url:        str,
    reason:     str,
    error_code: str = "EGRESS_TIMEOUT_BREACH",
) -> dict:
    """
    Standardised error response returned whenever Crawl4AI fails.

    error_code values
    -----------------
    EGRESS_TIMEOUT_BREACH   — proxy dropout, navigation timeout
    SESSION_LOAD_FAILURE    — credential vault unreachable
    CRAWL4AI_ERROR          — unexpected Crawl4AI runtime exception
    CACHE_WRITE_FAILURE     — Redis unavailable (non-fatal, content still returned)
    """
    return {
        "status":     "error",
        "task_id":    task_id,
        "url":        url,
        "error_code": error_code,
        "reason":     reason,
        "timestamp":  time.time(),
    }


# ===========================================================================
# FastMCP initialisation
# ===========================================================================

mcp = FastMCP(
    "DeepWebOrchestrator",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ===========================================================================
# Pydantic schemas for the MCP tool
# ===========================================================================

class FetchDeepWebDataInput(BaseModel):
    """
    Input contract for the fetch_deep_web_data MCP tool.

    Fields
    ------
    target_database : str
        Valid URL (https://...) or .onion hidden service address.
        The crawler will navigate to this endpoint and extract its full
        DOM content as sanitized markdown.

    thread_id : str
        Active session thread identifier.  Used as the primary key to look
        up JWT arrays and persistent cookies from the local SQLite
        CredentialVault (AES-256 encrypted).  Defaults to "default".

    session_required : bool
        When True, the tool pauses its execution loop and queries the
        CredentialVault for stored authentication tokens keyed by thread_id
        before issuing any navigation command to the browser context.
        Tokens are injected into the Chromium context via
        on_page_context_created before the first HTTP request is sent.

    use_tor_network : bool
        When True, routes all browser traffic through the local Privoxy
        HTTP→SOCKS5 bridge at http://tor-gateway:8118, which forwards
        traffic to the Tor SOCKS5 daemon.  Required for .onion addresses.

    js_eval : str | None
        Optional JavaScript expression to evaluate on the page after the
        DOM reaches DOMContentLoaded state.  Useful for triggering lazy-
        loaded content or SPA routing.

    search_query : str | None
        Optional search query to pass to the SearXNG engine after DOM
        extraction (supplements raw page content with search results).
    """

    target_database:  str            = Field(..., description="Target URL or .onion address.")
    thread_id:        str            = Field("default", description="Session thread ID for credential lookup.")
    session_required: bool           = Field(False, description="Pause loop and load session credentials from vault.")
    use_tor_network:  bool           = Field(False, description="Route via Privoxy/Tor HTTP proxy.")
    js_eval:          Optional[str]  = Field(None, description="Optional JS to evaluate post-load.")
    search_query:     Optional[str]  = Field(None, description="Optional SearXNG search query.")


# ===========================================================================
# Crawl4AI extraction engine
# ===========================================================================

async def _crawl4ai_extract(
    url:              str,
    thread_id:        str,
    session_required: bool,
    use_tor:          bool,
    js_eval:          Optional[str],
    task_id:          str,
) -> dict:
    """
    Core extraction coroutine backed by Crawl4AI's AsyncWebCrawler.

    Steps
    -----
    1. If session_required: pull stored credentials from CredentialVault.
    2. Build BrowserConfig with:
       - Privoxy HTTP proxy when use_tor=True.
       - Cookie / JWT header injection via on_page_context_created hook.
    3. Run AsyncWebCrawler.arun() targeting the URL.
    4. Post-process with TextSanitizer (MAX_CHARS=20 000).
    5. Return structured result dict.

    All Crawl4AI / browser errors are caught and returned as error blocks.
    """

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
        from crawl4ai.content_filter_strategy import PruningContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    except ImportError as exc:
        logger.error("[CRAWL4AI] Import failure: %s", exc)
        return _error_block(task_id, url, f"Crawl4AI not installed: {exc}", "CRAWL4AI_ERROR")

    # ── Step 1: Credential vault lookup ────────────────────────────────────
    cookies_payload:  list = []
    jwt_bearer_token: Optional[str] = None

    if session_required:
        _set_task_progress(task_id, 5, "running")
        logger.info(
            "[CRAWL4AI] session_required=True — loading credentials for thread_id=%r", thread_id
        )
        try:
            creds = get_credentials(thread_id)
            if creds and creds.get("payload"):
                auth_array = creds["payload"]
                if not isinstance(auth_array, list):
                    auth_array = [auth_array]

                for entry in auth_array:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("name", "").startswith("__jwt__"):
                        jwt_bearer_token = entry.get("value", "") or jwt_bearer_token
                    elif entry.get("name") and entry.get("value") and entry.get("domain"):
                        cookies_payload.append(entry)

                logger.info(
                    "[CRAWL4AI] Loaded %d cookie(s), JWT=%s for thread_id=%r.",
                    len(cookies_payload),
                    "yes" if jwt_bearer_token else "no",
                    thread_id,
                )
            else:
                logger.warning(
                    "[CRAWL4AI] No credentials found for thread_id=%r — "
                    "proceeding unauthenticated.",
                    thread_id,
                )
        except Exception as exc:
            logger.error(
                "[CRAWL4AI] CredentialVault lookup failed for thread_id=%r: %s",
                thread_id, exc,
            )
            return _error_block(
                task_id, url,
                f"Credential vault unreachable: {exc}",
                "SESSION_LOAD_FAILURE",
            )
    else:
        logger.debug("[CRAWL4AI] session_required=False — skipping credential lookup.")

    _set_task_progress(task_id, 10, "running")

    # ── Step 2: Build BrowserConfig ────────────────────────────────────────
    #
    # Crawl4AI BrowserConfig fields used:
    #   headless=True             — no display required in container
    #   proxy_config              — Privoxy HTTP proxy when use_tor=True
    #   headers                   — JWT Authorization header injection
    #   cookies                   — Persistent cookie list from CredentialVault
    #   ignore_https_errors=True  — required for .onion self-signed certs
    #   page_timeout              — navigation timeout (ms)
    #   use_managed_browser=False — use spawned Chromium, not persistent profile
    #
    # on_page_context_created hook:
    # Crawl4AI v0.6+ exposes `on_page_context_created` as a BrowserConfig kwarg.
    # The hook fires after the browser context is created but before any navigation
    # is issued — exactly where we need to inject auth state.
    # ---------------------------------------------------------------------------

    # Build headers dict — JWT goes here as Authorization header
    extra_headers: dict = {
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    if jwt_bearer_token:
        extra_headers["Authorization"] = f"Bearer {jwt_bearer_token}"
        logger.info("[CRAWL4AI] JWT Authorization header injected.")

    # Crawl4AI cookie format: list of dicts with name/value/domain/path
    crawl_cookies = [
        {
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ""),
            "path":     c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
        }
        for c in cookies_payload
        if c.get("name") and c.get("value")
    ]

    # Proxy configuration
    proxy_config: Optional[dict] = None
    if use_tor:
        proxy_config = {"server": TOR_HTTP_PROXY}
        logger.info("[CRAWL4AI] Tor HTTP proxy enabled: %s", TOR_HTTP_PROXY)

    # Chromium launch args for air-gapped container operation
    browser_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
    ]

    try:
        browser_config = BrowserConfig(
            headless=True,
            browser_type="chromium",
            ignore_https_errors=True,
            extra_args=browser_args,
            headers=extra_headers,
            cookies=crawl_cookies if crawl_cookies else None,
            proxy_config=proxy_config,
            page_timeout=PAGE_TIMEOUT_MS,
            use_managed_browser=False,
            verbose=False,
        )
    except Exception as exc:
        # Older versions of Crawl4AI may not support all kwargs — adapt gracefully
        logger.warning(
            "[CRAWL4AI] BrowserConfig init with full kwargs failed (%s); "
            "retrying with minimal config.", exc,
        )
        try:
            browser_config = BrowserConfig(
                headless=True,
                ignore_https_errors=True,
                extra_args=browser_args,
                proxy_config=proxy_config,
            )
        except Exception as exc2:
            return _error_block(
                task_id, url,
                f"BrowserConfig init failed: {exc2}",
                "CRAWL4AI_ERROR",
            )

    # ── Step 3: Build CrawlerRunConfig ─────────────────────────────────────
    #
    # CrawlerRunConfig governs per-run behaviour:
    #   cache_mode=BYPASS          — never serve stale cache for auth'd sessions
    #   word_count_threshold=10    — skip stub pages
    #   markdown_generator         — DefaultMarkdownGenerator strips nav/footer
    #   js_code                    — optional JS injection after DOMContentLoaded
    #   wait_for                   — CSS selector to wait for before extraction
    #   page_timeout               — ms before navigation is aborted
    # ---------------------------------------------------------------------------

    markdown_gen = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(
            threshold=0.45,         # prune low-information density blocks
            threshold_type="fixed",
            min_word_threshold=5,
        ),
        options={"ignore_links": False},
    )

    js_code: Optional[str] = js_eval

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=10,
        markdown_generator=markdown_gen,
        js_code=js_code,
        page_timeout=PAGE_TIMEOUT_MS,
        verbose=False,
        # on_page_context_created is passed here as a run-level hook.
        # It fires after BrowserContext is created, before goto().
        # We use it to log auth injection confirmation inside the crawl lifecycle.
        # Actual cookie/header injection is handled via browser_config.cookies
        # and browser_config.headers above (Crawl4AI's preferred mechanism).
    )

    _set_task_progress(task_id, 20, "running")

    # ── Step 4: Run AsyncWebCrawler ────────────────────────────────────────
    logger.info(
        "[CRAWL4AI] Starting crawl — task_id=%s  url=%r  tor=%s  session=%s",
        task_id, url, use_tor, session_required,
    )

    crawler_result = None
    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            _set_task_progress(task_id, 30, "running")

            crawler_result = await crawler.arun(
                url=url,
                config=run_config,
            )
            _set_task_progress(task_id, 75, "running")

    except asyncio.TimeoutError as exc:
        reason = f"Navigation timeout after {PAGE_TIMEOUT_MS}ms: {exc}"
        logger.warning("[CRAWL4AI] TimeoutError (task_id=%s): %s", task_id, reason)
        return _error_block(task_id, url, reason, "EGRESS_TIMEOUT_BREACH")

    except Exception as exc:
        msg = str(exc)
        # Classify common Tor / anti-bot failure signatures
        if any(sig in msg for sig in (
            "ERR_SOCKS_CONNECTION_FAILED",
            "ERR_TUNNEL_CONNECTION_FAILED",
            "ERR_PROXY_CONNECTION_FAILED",
            "net::ERR_SOCKS_",
        )):
            code = "EGRESS_TIMEOUT_BREACH"
        elif "ERR_NAME_NOT_RESOLVED" in msg or "ERR_CONNECTION_REFUSED" in msg:
            code = "EGRESS_TIMEOUT_BREACH"
        elif "403" in msg or "429" in msg or "cloudflare" in msg.lower():
            code = "EGRESS_TIMEOUT_BREACH"
        else:
            code = "CRAWL4AI_ERROR"

        logger.error("[CRAWL4AI] Extraction failed (%s) task_id=%s: %s", code, task_id, msg)
        return _error_block(task_id, url, msg, code)

    # ── Validate crawl result ──────────────────────────────────────────────
    if crawler_result is None or not crawler_result.success:
        reason = getattr(crawler_result, "error_message", "Crawl4AI returned no result.")
        logger.warning("[CRAWL4AI] Unsuccessful crawl (task_id=%s): %s", task_id, reason)
        return _error_block(task_id, url, reason, "EGRESS_TIMEOUT_BREACH")

    _set_task_progress(task_id, 80, "running")

    # ── Step 5: Extract markdown content ──────────────────────────────────
    # Prefer fit_markdown (pruned) → markdown_v2 → raw_markdown → cleaned_html fallback
    raw_md: str = (
        getattr(crawler_result.markdown_v2, "fit_markdown", None)
        or getattr(crawler_result.markdown_v2, "raw_markdown", None)
        or getattr(crawler_result, "markdown",  None)
        or getattr(crawler_result, "cleaned_html", None)
        or ""
    )

    if not raw_md.strip():
        logger.warning(
            "[CRAWL4AI] Empty markdown output for url=%r (task_id=%s). "
            "Page may be JavaScript-rendered without sufficient hydration time.",
            url, task_id,
        )
        # Return the raw HTML as fallback content rather than empty
        raw_md = getattr(crawler_result, "html", "") or ""

    _set_task_progress(task_id, 88, "running")

    # ── Step 6: TextSanitizer pipeline ────────────────────────────────────
    clean = _sanitizer.sanitize(raw_md)
    logger.info(
        "[CRAWL4AI] Sanitized output — raw_len=%d  clean_len=%d  task_id=%s",
        len(raw_md), len(clean), task_id,
    )

    _set_task_progress(task_id, 95, "running")

    result = {
        "status":      "success",
        "task_id":     task_id,
        "url":         url,
        "content":     clean,
        "http_status": getattr(crawler_result, "status_code", None),
        "links_found": len(getattr(crawler_result, "links", {}).get("internal", [])),
        "truncated":   len(raw_md) > MAX_CHARS,
        "timestamp":   time.time(),
    }

    # Mark task complete in registry
    entry = _task_registry.get(task_id)
    if entry:
        entry.progress = 100
        entry.status   = "done"
        entry.result   = result

    return result


# ===========================================================================
# MCP TOOL 1 — fetch_deep_web_data
# ===========================================================================

@mcp.tool()
async def fetch_deep_web_data(
    target_database:  str,
    thread_id:        str  = "default",
    session_required: bool = False,
    use_tor_network:  bool = False,
    js_eval:          str  = None,
) -> str:
    """
    Extract and sanitize content from deep web portals, SPAs, or .onion services.

    Backed by Crawl4AI's AsyncWebCrawler with authenticated Chromium rendering.

    The tool performs a complete extraction pipeline:
      1. Cache probe (SHA-256 keyed, 1-hour TTL).
      2. Optional CredentialVault lookup (JWT + cookies) when session_required=True.
      3. Chromium browser context hydration via on_page_context_created hook.
      4. Optional Privoxy HTTP proxy routing (Tor) via use_tor_network=True.
      5. Markdown DOM extraction with structural boilerplate removal.
      6. TextSanitizer pipeline: base64 strip → nested table strip → MAX_CHARS=20 000.

    Parameters
    ----------
    target_database : str
        Valid URL or .onion hidden service address to extract.

    thread_id : str
        Active session thread identifier used to look up authentication
        tokens from the local SQLite CredentialVault.

    session_required : bool
        If True, pause the execution loop and query the CredentialVault for
        stored JWTs and persistent cookies before any browser navigation.

    use_tor_network : bool
        If True, route all browser traffic through the local Privoxy HTTP
        bridge (http://tor-gateway:8118) for anonymised egress via Tor.
        Required for .onion address resolution.

    js_eval : str | None
        Optional JavaScript expression evaluated on the page after load.
        Use to trigger lazy-rendered SPA content or bypass soft paywalls.

    Returns
    -------
    str
        JSON-serialized result with keys:
          status, task_id, url, content, http_status, links_found, truncated
        or on failure:
          status, task_id, url, error_code, reason
    """
    # ── Cache probe ────────────────────────────────────────────────────────
    sig      = f"{target_database}|{thread_id}|{session_required}|{use_tor_network}|{js_eval}"
    url_hash = hashlib.sha256(sig.encode()).hexdigest()
    cache_key = f"mcp_cache:{url_hash}"

    try:
        cached = redis_client.get(cache_key)
    except Exception:
        cached = None

    if cached:
        logger.info("[MCP] Cache hit — hash=%s", url_hash[:12])
        return json.dumps({
            "status":  "success",
            "source":  "cache",
            "content": cached,
        })

    # ── Register task ──────────────────────────────────────────────────────
    task_id = str(uuid.uuid4())
    _task_registry[task_id] = ExtractionTask(
        task_id=task_id,
        url=target_database,
        progress=0,
        status="running",
    )

    # ── Run extraction ─────────────────────────────────────────────────────
    result = await _crawl4ai_extract(
        url=target_database,
        thread_id=thread_id,
        session_required=session_required,
        use_tor=use_tor_network,
        js_eval=js_eval,
        task_id=task_id,
    )

    if result["status"] != "success":
        return json.dumps(result)

    # ── Cache successful result ────────────────────────────────────────────
    try:
        redis_client.setex(cache_key, 3600, result["content"])
    except Exception as exc:
        logger.warning("[MCP] Redis cache write failed (non-fatal): %s", exc)

    return json.dumps(result)


# ===========================================================================
# MCP TOOL 2 — search_deep_web_database  (unchanged from v2)
# ===========================================================================

@mcp.tool()
async def search_deep_web_database(
    target_database:  str,
    search_query:     str,
    session_required: bool = False,
    use_tor_network:  bool = False,
) -> str:
    """
    Search specific deep web databases or academic registries via SearXNG.

    Parameters
    ----------
    target_database : str
        SearXNG engine identifier (e.g. "ahmia", "torch", "google").
    search_query : str
        The query string to submit.
    session_required : bool
        Reserved for future authenticated SearXNG instances.
    use_tor_network : bool
        Reserved — SearXNG is always routed internally.
    """
    import httpx

    sig        = f"{target_database}|{search_query}|{use_tor_network}"
    query_hash = hashlib.sha256(sig.encode()).hexdigest()
    cache_key  = f"searxng_cache:{query_hash}"

    try:
        cached = redis_client.get(cache_key)
    except Exception:
        cached = None

    if cached:
        return json.dumps({
            "status":  "success",
            "source":  "cache",
            "results": json.loads(cached),
        })

    params = {"q": search_query, "engines": target_database, "format": "json"}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{SEARXNG_URL}/search", params=params, timeout=30.0
            )
            resp.raise_for_status()
            data = resp.json()
            try:
                redis_client.setex(
                    cache_key, 3600,
                    json.dumps(data.get("results", [])),
                )
            except Exception:
                pass
            return json.dumps({"status": "success", "results": data.get("results", [])})
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)})


# ===========================================================================
# FastAPI application  (MCP SSE transport + streaming endpoints)
# ===========================================================================

app: FastAPI = mcp.sse_app()

# ---------------------------------------------------------------------------
# Starlette SseServerTransport — MCP /sse discovery route
#
# This mounts the canonical MCP SSE wire protocol at /sse so MCP-compatible
# clients (Open-WebUI, Claude Desktop, Cursor) can discover and invoke tools.
# The endpoint supports real-time event streaming of tool execution state.
# ---------------------------------------------------------------------------

sse_transport = SseServerTransport("/sse")


@app.get("/sse")
async def mcp_sse_endpoint(request: Request):
    """
    MCP SSE transport route.

    Streams tool discovery metadata and execution state events to any
    MCP-compatible client connected to this endpoint.  Events include:
      - tools/list response (on connect)
      - tool call progress updates
      - tool call result payloads

    Compatible with Open-WebUI's MCP client, Claude Desktop, and the
    official MCP Python SDK test client.
    """
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp._mcp_server.run(
            streams[0],
            streams[1],
            mcp._mcp_server.create_initialization_options(),
        )


# ---------------------------------------------------------------------------
# Request schema for /extract/stream
# ---------------------------------------------------------------------------

class ExtractRequest(BaseModel):
    """Request body for the /extract/stream SSE endpoint."""
    url:              str
    thread_id:        str  = Field("default", description="Session thread ID.")
    session_required: bool = Field(False,     description="Load credentials from vault.")
    use_tor_network:  bool = Field(False,     description="Route via Tor HTTP proxy.")
    js_eval:          Optional[str] = Field(None, description="JS to evaluate post-load.")


# ===========================================================================
# ENDPOINT: POST /extract/stream
# ===========================================================================

@app.post("/extract/stream")
async def extract_stream(req: ExtractRequest):
    """
    Launch a Crawl4AI extraction task and stream SSE progress frames.

    The extraction runs as a background asyncio.Task.  The SSE generator
    polls the shared task registry at 250 ms intervals, emitting progress
    events until the task reaches status 'done' or 'error'.

    Frame schema
    ------------
      event: progress  data: {"task_id":str, "progress":0-100, "status":str, "url":str}
      event: result    data: {"task_id":str, "content":str, "source":"live"|"cache"}
      event: error     data: {"task_id":str, "error_code":str, "reason":str}
    """
    task_id   = str(uuid.uuid4())
    sig       = f"{req.url}|{req.thread_id}|{req.session_required}|{req.use_tor_network}|{req.js_eval}"
    url_hash  = hashlib.sha256(sig.encode()).hexdigest()
    cache_key = f"mcp_cache:{url_hash}"

    # Seed the registry immediately so the generator can read it from frame 0
    _task_registry[task_id] = ExtractionTask(
        task_id=task_id,
        url=req.url,
        progress=0,
        status="pending",
    )

    # ── Fast-path cache hit ────────────────────────────────────────────────
    try:
        cached = redis_client.get(cache_key)
    except Exception:
        cached = None

    if cached:
        async def _cached_stream():
            yield {
                "event": "progress",
                "data":  json.dumps({
                    "task_id":  task_id,
                    "progress": 100,
                    "status":   "done",
                    "url":      req.url,
                }),
            }
            yield {
                "event": "result",
                "data":  json.dumps({
                    "task_id": task_id,
                    "content": cached,
                    "source":  "cache",
                }),
            }
        return EventSourceResponse(_cached_stream())

    # ── Background extraction task ─────────────────────────────────────────
    async def _run_extraction() -> dict:
        result = await _crawl4ai_extract(
            url=req.url,
            thread_id=req.thread_id,
            session_required=req.session_required,
            use_tor=req.use_tor_network,
            js_eval=req.js_eval,
            task_id=task_id,
        )
        if result.get("status") == "success":
            try:
                redis_client.setex(cache_key, 3600, result["content"])
            except Exception:
                pass
        return result

    bg_task = asyncio.create_task(_run_extraction())

    async def _sse_generator():
        last_pct = -1

        # Frame 0: task_id announcement + initial state
        yield {
            "event": "progress",
            "data":  json.dumps({
                "task_id":  task_id,
                "progress": 0,
                "status":   "running",
                "url":      req.url,
            }),
        }

        # Poll until task completes
        while not bg_task.done():
            await asyncio.sleep(0.25)
            entry = _task_registry.get(task_id)
            if entry and entry.progress != last_pct:
                last_pct = entry.progress
                yield {
                    "event": "progress",
                    "data":  json.dumps({
                        "task_id":  task_id,
                        "progress": entry.progress,
                        "status":   entry.status,
                    }),
                }

        # Retrieve final result
        try:
            result = await bg_task
        except Exception as exc:
            yield {
                "event": "error",
                "data":  json.dumps({
                    "task_id":    task_id,
                    "error_code": "TASK_EXCEPTION",
                    "reason":     str(exc),
                }),
            }
            return

        if result.get("status") == "success":
            yield {
                "event": "progress",
                "data":  json.dumps({
                    "task_id":  task_id,
                    "progress": 100,
                    "status":   "done",
                }),
            }
            yield {
                "event": "result",
                "data":  json.dumps({
                    "task_id":     task_id,
                    "content":     result.get("content", ""),
                    "source":      "live",
                    "truncated":   result.get("truncated", False),
                    "links_found": result.get("links_found", 0),
                }),
            }
        else:
            yield {
                "event": "error",
                "data":  json.dumps({
                    "task_id":    task_id,
                    "error_code": result.get("error_code", "UNKNOWN"),
                    "reason":     result.get("reason", "Unknown extraction failure."),
                }),
            }

    return EventSourceResponse(_sse_generator())


# ===========================================================================
# ENDPOINT: GET /extract/status/{task_id}
# Polling fallback for SSE-incapable clients.
# ===========================================================================

@app.get("/extract/status/{task_id}")
async def extract_status(task_id: str):
    """Return current extraction task progress without SSE."""
    task = _get_task(task_id)
    if not task:
        return {"error": f"No task found for task_id={task_id!r}"}
    return {
        "task_id":    task.task_id,
        "url":        task.url,
        "progress":   task.progress,
        "status":     task.status,
        "started_at": task.started_at,
    }


# ===========================================================================
# ENDPOINT: POST /credentials/store
# Store or rotate auth credentials (JWT arrays / cookies) for a thread_id.
# ===========================================================================

class CredentialStoreRequest(BaseModel):
    thread_id:  str
    auth_array: list        # list of cookie dicts or {"name": "__jwt__", "value": "..."}
    proxy_node: Optional[str] = None


@app.post("/credentials/store")
async def store_credentials(req: CredentialStoreRequest):
    """
    Encrypt and persist an authorization array (JWT / cookie list) for a
    given thread_id.  Consumed by the session_required lookup path.

    The payload is AES-256 encrypted via Fernet before storage in the
    local SQLite CredentialVault.
    """
    try:
        save_credentials(
            domain_id=req.thread_id,
            payload=req.auth_array,
            proxy_node=req.proxy_node,
        )
        return {
            "status":    "ok",
            "thread_id": req.thread_id,
            "entries":   len(req.auth_array),
        }
    except Exception as exc:
        logger.exception("[CREDENTIALS] Store failed for thread_id=%r.", req.thread_id)
        return {"status": "error", "reason": str(exc)}


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    import uvicorn
    os.makedirs("./data", exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
