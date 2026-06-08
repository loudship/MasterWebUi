"""
extractor.py — Playwright Hydration Engine
===========================================

Provides `PlaywrightExtractor`, a high-performance async extraction class
that interfaces directly with an internal Playwright headless Chromium
instance (or the Browserless WebSocket endpoint when available).

Key behaviours
--------------
1. `on_page_context_created` lifecycle hook:
   - Fires before every primary page navigation command.
   - Queries `database.get_credentials(thread_id)` to extract active
     authorization arrays (JWT arrays, authentication cookies).
   - Iterates the decrypted auth array and injects each entry into the
     live browser context via `context.add_cookies()`.

2. Tor SOCKS5 proxy routing:
   - When `use_tor=True`, routes all traffic through
     `socks5://tor-gateway:9050`.
   - On anti-bot trap / network-layer drop (`TimeoutError`, `Error`):
     aborts the page, closes the context, frees shared memory, and returns
     a standardised JSON error block.

3. Async task tracking:
   - Every `extract()` call generates a unique `task_id`.
   - Progress callbacks update a shared asyncio.Event-backed registry so
     the SSE stream endpoint can read current completion percentages.

4. No WAN dependency: all network traffic is routed through the local
   Docker stack (Tor gateway, Browserless, redis-cache).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Prefer Browserless WS endpoint; fall back to local chromium spawn
BROWSERLESS_WS: str = os.environ.get("BROWSERLESS_WS", "ws://browserless:3000")
USE_BROWSERLESS: bool = os.environ.get("USE_BROWSERLESS", "true").lower() == "true"

TOR_SOCKS_HOST: str = os.environ.get("TOR_SOCKS_HOST", "tor-gateway")
TOR_SOCKS_PORT: int = int(os.environ.get("TOR_SOCKS_PORT", "9050"))

# Page navigation timeout in milliseconds
PAGE_TIMEOUT_MS: int = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))

# ---------------------------------------------------------------------------
# Standardised error block
# ---------------------------------------------------------------------------

def _nav_error_block(
    task_id:   str,
    url:       str,
    reason:    str,
    error_code: str = "NAVIGATION_TIMEOUT",
) -> dict:
    """
    Standardised JSON error block returned on Tor network drops, anti-bot
    traps, or any navigation-layer failure.
    """
    return {
        "status":     "error",
        "task_id":    task_id,
        "url":        url,
        "error_code": error_code,
        "reason":     reason,
        "timestamp":  time.time(),
    }


# ---------------------------------------------------------------------------
# Task progress registry
# ---------------------------------------------------------------------------

@dataclass
class ExtractionTask:
    task_id:    str
    url:        str
    progress:   int  = 0          # 0–100
    status:     str  = "pending"  # pending | running | done | error
    result:     Optional[dict] = None
    started_at: float = field(default_factory=time.time)

_task_registry: dict[str, ExtractionTask] = {}


def get_task(task_id: str) -> Optional[ExtractionTask]:
    return _task_registry.get(task_id)


def _update_task(task_id: str, **kwargs: Any) -> None:
    task = _task_registry.get(task_id)
    if task:
        for k, v in kwargs.items():
            setattr(task, k, v)


# ---------------------------------------------------------------------------
# PlaywrightExtractor
# ---------------------------------------------------------------------------

class PlaywrightExtractor:
    """
    Async Playwright-based web extraction engine with:
      - on_page_context_created lifecycle hook for session hydration
      - Tor SOCKS5 proxy support
      - Anti-bot / network-drop error catching
      - Task-ID-based progress tracking

    Usage
    -----
    extractor = PlaywrightExtractor()
    result = await extractor.extract(
        url="https://example.onion",
        thread_id="user-abc",
        use_tor=True,
    )
    """

    # ------------------------------------------------------------------
    # Lifecycle hook: on_page_context_created
    # ------------------------------------------------------------------

    async def _on_page_context_created(
        self,
        context,          # playwright BrowserContext
        thread_id: str,
        task_id:   str,
    ) -> None:
        """
        Fires before every primary page navigation command.

        1. Queries the local SQLite auth vault via thread_id.
        2. Decrypts the auth payload (handled by database.decrypt_payload).
        3. Iterates the authorization array and injects each entry into
           the browser context via native cookie injection.

        Supported array entry formats
        ------------------------------
        Cookie dict:
            {"name": "session", "value": "abc", "domain": ".example.com",
             "path": "/", "httpOnly": True, "secure": True}

        JWT bearer (stored as a synthetic cookie for injection):
            {"name": "__jwt__", "value": "eyJ...", "domain": ".example.com",
             "path": "/"}
            → additionally sets Authorization header via route interception.
        """
        # Import here to avoid circular import at module level
        from database import get_credentials

        try:
            creds = get_credentials(thread_id)
        except Exception as exc:
            logger.warning(
                "[EXTRACTOR] on_page_context_created: credential lookup failed "
                "for thread_id=%r: %s",
                thread_id, exc,
            )
            return

        if not creds or not creds.get("payload"):
            logger.debug(
                "[EXTRACTOR] on_page_context_created: no credentials found "
                "for thread_id=%r — proceeding unauthenticated.",
                thread_id,
            )
            return

        auth_array: list = creds["payload"]
        if not isinstance(auth_array, list):
            # Handle single-credential dict wrapped without an outer list
            auth_array = [auth_array]

        cookies_to_inject: list[dict] = []
        jwt_tokens: list[str] = []

        for entry in auth_array:
            if not isinstance(entry, dict):
                continue

            # Detect JWT bearer token entries (convention: name == "__jwt__")
            if entry.get("name", "").startswith("__jwt__"):
                jwt = entry.get("value", "")
                if jwt:
                    jwt_tokens.append(jwt)
                    logger.debug(
                        "[EXTRACTOR] JWT bearer token queued for header injection "
                        "(thread_id=%r).", thread_id,
                    )
            else:
                # Standard cookie: must have at least name + value + domain
                if entry.get("name") and entry.get("value") and entry.get("domain"):
                    cookies_to_inject.append(entry)

        # ── Inject cookies into browser context ───────────────────────────
        if cookies_to_inject:
            try:
                await context.add_cookies(cookies_to_inject)
                logger.info(
                    "[EXTRACTOR] Injected %d cookie(s) into browser context "
                    "(thread_id=%r  task_id=%s).",
                    len(cookies_to_inject), thread_id, task_id,
                )
            except Exception as exc:
                logger.warning(
                    "[EXTRACTOR] Cookie injection failed (thread_id=%r): %s",
                    thread_id, exc,
                )

        # ── Inject JWT bearers via route interception ─────────────────────
        if jwt_tokens:
            # Use the first JWT; multi-token scenarios are uncommon
            bearer = jwt_tokens[0]
            try:
                async def _add_auth_header(route, request):
                    headers = {**request.headers, "Authorization": f"Bearer {bearer}"}
                    await route.continue_(headers=headers)

                await context.route("**/*", _add_auth_header)
                logger.info(
                    "[EXTRACTOR] JWT Authorization header route interceptor installed "
                    "(thread_id=%r  task_id=%s).",
                    thread_id, task_id,
                )
            except Exception as exc:
                logger.warning(
                    "[EXTRACTOR] JWT route interception failed (thread_id=%r): %s",
                    thread_id, exc,
                )

    # ------------------------------------------------------------------
    # Primary extraction coroutine
    # ------------------------------------------------------------------

    async def extract(
        self,
        url:       str,
        thread_id: str,
        use_tor:   bool = False,
        js_script: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> dict:
        """
        Navigate to *url*, hydrate session credentials, and extract clean
        DOM text content.

        Parameters
        ----------
        url       : Target URL.
        thread_id : Identifies the requesting user/session; used for auth lookup.
        use_tor   : Route traffic through SOCKS5 Tor proxy when True.
        js_script : Optional JavaScript to evaluate on the page after load.
        on_progress: Optional callback(pct: int) invoked at each milestone.

        Returns
        -------
        dict with keys: status, task_id, url, raw (on success)
                     or status, task_id, url, error_code, reason (on failure)
        """
        # Lazily import playwright to avoid import-time crash if not installed
        try:
            from playwright.async_api import (
                async_playwright,
                TimeoutError as PlaywrightTimeoutError,
                Error as PlaywrightError,
            )
        except ImportError as exc:
            logger.error("[EXTRACTOR] Playwright not installed: %s", exc)
            task_id = str(uuid.uuid4())
            return _nav_error_block(task_id, url, f"Playwright import error: {exc}", "IMPORT_ERROR")

        task_id = str(uuid.uuid4())
        task    = ExtractionTask(task_id=task_id, url=url, status="running")
        _task_registry[task_id] = task

        def _progress(pct: int) -> None:
            _update_task(task_id, progress=pct)
            if on_progress:
                on_progress(pct)

        _progress(0)
        logger.info(
            "[EXTRACTOR] Starting extraction — task_id=%s  url=%r  use_tor=%s  thread_id=%r",
            task_id, url, use_tor, thread_id,
        )

        # ── Proxy configuration ───────────────────────────────────────────
        proxy_settings: Optional[dict] = None
        if use_tor:
            proxy_settings = {
                "server": f"socks5://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}",
            }
            logger.info("[EXTRACTOR] Tor SOCKS5 proxy enabled: %s", proxy_settings["server"])

        context = None
        page    = None

        async with async_playwright() as pw:
            try:
                _progress(5)

                # ── Launch browser ────────────────────────────────────────
                if USE_BROWSERLESS:
                    browser = await pw.chromium.connect_over_cdp(BROWSERLESS_WS)
                    logger.info("[EXTRACTOR] Connected to Browserless CDP: %s", BROWSERLESS_WS)
                else:
                    browser = await pw.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                        ],
                    )
                    logger.info("[EXTRACTOR] Local Chromium spawned.")

                _progress(10)

                # ── Create browser context ────────────────────────────────
                context_kwargs: dict = {
                    "user_agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "ignore_https_errors": True,
                    "java_script_enabled": True,
                }
                if proxy_settings:
                    context_kwargs["proxy"] = proxy_settings

                context = await browser.new_context(**context_kwargs)

                # ── on_page_context_created lifecycle hook ────────────────
                # Fires before the primary navigation — injects auth credentials
                # into the live browser context.
                _progress(15)
                await self._on_page_context_created(context, thread_id, task_id)
                _progress(20)

                # ── Open page and navigate ────────────────────────────────
                page = await context.new_page()
                page.set_default_timeout(PAGE_TIMEOUT_MS)

                _progress(25)
                logger.info("[EXTRACTOR] Navigating to %r (timeout=%dms).", url, PAGE_TIMEOUT_MS)

                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT_MS,
                )

                _progress(50)

                # Check for anti-bot HTTP status codes (403, 429, 503, Cloudflare)
                if response and response.status in (403, 429, 503):
                    raise PlaywrightError(
                        f"Anti-bot HTTP {response.status} received from {url}"
                    )

                # ── Wait for network to settle ────────────────────────────
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeoutError:
                    # networkidle timeout is non-fatal; content may still be usable
                    logger.debug("[EXTRACTOR] networkidle timeout (non-fatal) for %r.", url)

                _progress(65)

                # ── Optional JS evaluation ────────────────────────────────
                if js_script:
                    try:
                        await page.evaluate(js_script)
                        logger.debug("[EXTRACTOR] Custom JS evaluated for task_id=%s.", task_id)
                    except PlaywrightError as exc:
                        logger.warning("[EXTRACTOR] JS evaluation error (non-fatal): %s", exc)

                _progress(75)

                # ── Extract DOM text content ──────────────────────────────
                raw_text = await page.evaluate(
                    """() => {
                        // Remove purely decorative / invisible elements
                        const remove = ['script', 'style', 'noscript',
                                        'svg', 'canvas', 'iframe'];
                        remove.forEach(tag => {
                            document.querySelectorAll(tag).forEach(el => el.remove());
                        });
                        return document.body ? document.body.innerText : document.documentElement.innerText;
                    }"""
                )
                _progress(90)

                logger.info(
                    "[EXTRACTOR] Extraction complete — task_id=%s  raw_len=%d",
                    task_id, len(raw_text or ""),
                )

                result = {
                    "status":    "success",
                    "task_id":   task_id,
                    "url":       url,
                    "raw":       raw_text or "",
                    "http_status": response.status if response else None,
                    "timestamp": time.time(),
                }
                _update_task(task_id, progress=100, status="done", result=result)
                return result

            # ── Anti-bot trap / Tor network drop / navigation timeout ─────
            except PlaywrightTimeoutError as exc:
                reason = f"Navigation timeout after {PAGE_TIMEOUT_MS}ms: {exc}"
                logger.warning("[EXTRACTOR] TimeoutError (task_id=%s): %s", task_id, reason)
                await self._abort_and_free(page, context)
                err = _nav_error_block(task_id, url, reason, "NAVIGATION_TIMEOUT")
                _update_task(task_id, progress=0, status="error", result=err)
                return err

            except PlaywrightError as exc:
                msg = str(exc)
                # Classify common anti-bot / Tor failure signatures
                if any(sig in msg for sig in ("net::ERR_SOCKS_", "ERR_TUNNEL_CONNECTION_FAILED")):
                    code = "TOR_PROXY_FAILURE"
                elif "403" in msg or "429" in msg or "Anti-bot" in msg:
                    code = "ANTI_BOT_DETECTED"
                elif "ERR_NAME_NOT_RESOLVED" in msg or "ERR_CONNECTION_REFUSED" in msg:
                    code = "DNS_OR_CONNECTION_REFUSED"
                else:
                    code = "PLAYWRIGHT_ERROR"
                logger.warning("[EXTRACTOR] PlaywrightError %s (task_id=%s): %s", code, task_id, msg)
                await self._abort_and_free(page, context)
                err = _nav_error_block(task_id, url, msg, code)
                _update_task(task_id, progress=0, status="error", result=err)
                return err

            except Exception as exc:
                reason = f"Unexpected extraction error: {type(exc).__name__}: {exc}"
                logger.exception("[EXTRACTOR] Unhandled exception (task_id=%s).", task_id)
                await self._abort_and_free(page, context)
                err = _nav_error_block(task_id, url, reason, "INTERNAL_ERROR")
                _update_task(task_id, progress=0, status="error", result=err)
                return err

    # ------------------------------------------------------------------
    # Abort helper: release shared memory on error
    # ------------------------------------------------------------------

    async def _abort_and_free(self, page, context) -> None:
        """
        Abort the active page navigation (if any) and close the browser
        context to free shared memory resources on error paths.
        """
        if page:
            try:
                await page.close()
                logger.debug("[EXTRACTOR] Page closed (abort path).")
            except Exception:
                pass
        if context:
            try:
                await context.close()
                logger.debug("[EXTRACTOR] Browser context closed (abort path).")
            except Exception:
                pass
