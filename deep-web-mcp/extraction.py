"""
extraction.py — Crawl4AI extraction engine, task registry, and error handling.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from database import get_credentials
from policy import (
    ALLOW_PUBLIC_TARGETS,
    ALLOWED_TARGET_HOSTS,
    MAX_CHARS,
    PAGE_TIMEOUT_MS,
    TASK_REGISTRY_TTL_S,
)
from sanitizer import TextSanitizer

logger = logging.getLogger(__name__)

_sanitizer = TextSanitizer(max_chars=MAX_CHARS)


def _assert_allowed_target(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("Target must be a valid http:// or https:// URL.")
    if hostname in ALLOWED_TARGET_HOSTS:
        return
    if hostname.endswith(".onion"):
        raise ValueError(".onion targets are not supported by this direct-extraction service.")
    if not ALLOW_PUBLIC_TARGETS:
        raise ValueError(
            f"Target host {hostname!r} is not in the internal allowlist: "
            f"{sorted(ALLOWED_TARGET_HOSTS)}"
        )
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError(f"Target host {hostname!r} could not be resolved: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(f"Public target {hostname!r} resolved to blocked address {address}.")


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------
@dataclass
class ExtractionTask:
    task_id:    str
    url:        str
    progress:   int          = 0
    status:     str          = "pending"
    result:     Optional[dict] = None
    started_at: float        = field(default_factory=time.time)


_task_registry: dict[str, ExtractionTask] = {}


def prune_task_registry(now: Optional[float] = None) -> int:
    """Evict finished tasks older than TASK_REGISTRY_TTL_S. Returns count removed."""
    now = time.time() if now is None else now
    expired = [
        tid for tid, task in _task_registry.items()
        if task.status in ("done", "error") and (now - task.started_at) > TASK_REGISTRY_TTL_S
    ]
    for tid in expired:
        del _task_registry[tid]
    return len(expired)


def get_task(task_id: str) -> Optional[ExtractionTask]:
    return _task_registry.get(task_id)


def set_task_progress(task_id: str, progress: int, status: str = "running") -> None:
    task = _task_registry.get(task_id)
    if task:
        task.progress = progress
        task.status   = status


def new_task(url: str) -> str:
    """Register a new extraction task and return its task_id."""
    task_id = str(uuid.uuid4())
    prune_task_registry()
    _task_registry[task_id] = ExtractionTask(task_id=task_id, url=url, progress=0, status="running")
    return task_id


# ---------------------------------------------------------------------------
# Standard error block
# ---------------------------------------------------------------------------
def error_block(
    task_id: str, url: str, reason: str, error_code: str = "EGRESS_TIMEOUT_BREACH"
) -> dict:
    result = {
        "status":     "error",
        "task_id":    task_id,
        "url":        url,
        "error_code": error_code,
        "reason":     reason,
        "timestamp":  time.time(),
    }
    task = _task_registry.get(task_id)
    if task:
        task.status = "error"
        task.result = result
    return result


# ---------------------------------------------------------------------------
# Crawl4AI extraction
# ---------------------------------------------------------------------------
async def crawl4ai_extract(
    url:              str,
    thread_id:        str,
    session_required: bool,
    js_eval:          Optional[str],
    task_id:          str,
) -> dict:
    """Core extraction coroutine backed by Crawl4AI's AsyncWebCrawler."""
    try:
        _assert_allowed_target(url)
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
        from crawl4ai.content_filter_strategy import PruningContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    except ImportError as exc:
        logger.error("[CRAWL4AI] Import failure: %s", exc)
        return error_block(task_id, url, f"Crawl4AI not installed: {exc}", "CRAWL4AI_ERROR")

    cookies_payload:  list = []
    jwt_bearer_token: Optional[str] = None

    if session_required:
        set_task_progress(task_id, 5, "running")
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
        except Exception as exc:
            return error_block(
                task_id, url, f"Credential vault unreachable: {exc}", "SESSION_LOAD_FAILURE"
            )

    set_task_progress(task_id, 10, "running")

    extra_headers: dict = {
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    if jwt_bearer_token:
        extra_headers["Authorization"] = f"Bearer {jwt_bearer_token}"

    crawl_cookies = [
        {
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", ""),
            "path":     c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", False),
        }
        for c in cookies_payload if c.get("name") and c.get("value")
    ]

    browser_args = [
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--disable-extensions", "--disable-background-networking",
        "--disable-sync", "--metrics-recording-only", "--mute-audio", "--no-first-run",
    ]

    try:
        browser_config = BrowserConfig(
            headless=True, browser_type="chromium", ignore_https_errors=True,
            extra_args=browser_args, headers=extra_headers,
            cookies=crawl_cookies if crawl_cookies else None,
            page_timeout=PAGE_TIMEOUT_MS, use_managed_browser=False, verbose=False,
        )
    except Exception:
        try:
            browser_config = BrowserConfig(
                headless=True, ignore_https_errors=True, extra_args=browser_args
            )
        except Exception as exc2:
            return error_block(task_id, url, f"BrowserConfig init failed: {exc2}", "CRAWL4AI_ERROR")

    markdown_gen = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(
            threshold=0.45, threshold_type="fixed", min_word_threshold=5
        ),
        options={"ignore_links": False},
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, word_count_threshold=10,
        markdown_generator=markdown_gen, js_code=js_eval,
        page_timeout=PAGE_TIMEOUT_MS, verbose=False,
    )
    set_task_progress(task_id, 20, "running")

    crawler_result = None
    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            set_task_progress(task_id, 30, "running")
            crawler_result = await crawler.arun(url=url, config=run_config)
            set_task_progress(task_id, 75, "running")
    except asyncio.TimeoutError as exc:
        return error_block(
            task_id, url, f"Navigation timeout after {PAGE_TIMEOUT_MS}ms: {exc}",
            "EGRESS_TIMEOUT_BREACH",
        )
    except Exception as exc:
        msg = str(exc)
        code = (
            "EGRESS_TIMEOUT_BREACH"
            if any(s in msg for s in ["ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED", "403", "429", "cloudflare"])
            else "CRAWL4AI_ERROR"
        )
        return error_block(task_id, url, msg, code)

    if crawler_result is None or not crawler_result.success:
        reason = getattr(crawler_result, "error_message", "Crawl4AI returned no result.")
        return error_block(task_id, url, reason, "EGRESS_TIMEOUT_BREACH")

    set_task_progress(task_id, 80, "running")

    markdown_result = getattr(crawler_result, "markdown", None)
    if isinstance(markdown_result, str):
        raw_md = markdown_result
    else:
        raw_md = (
            getattr(markdown_result, "fit_markdown", None)
            or getattr(markdown_result, "raw_markdown", None)
            or getattr(markdown_result, "markdown_with_citations", None)
            or getattr(crawler_result, "cleaned_html", None)
            or ""
        )
    if not raw_md.strip():
        raw_md = getattr(crawler_result, "html", "") or ""

    set_task_progress(task_id, 88, "running")

    clean = _sanitizer.sanitize(raw_md)
    logger.info(
        "[CRAWL4AI] Sanitized — raw_len=%d  clean_len=%d  task_id=%s",
        len(raw_md), len(clean), task_id,
    )
    set_task_progress(task_id, 95, "running")

    result = {
        "status":      "success",
        "task_id":     task_id,
        "url":         url,
        "content":     clean,
        "http_status": getattr(crawler_result, "status_code", None),
        "links_found": len(getattr(crawler_result, "links", {}).get("internal", [])),
        "truncated":   len(raw_md) > MAX_CHARS,
        "route":       "direct",
        "timestamp":   time.time(),
    }
    entry = _task_registry.get(task_id)
    if entry:
        entry.progress = 100
        entry.status   = "done"
        entry.result   = result
    return result
