"""Zero-trust web discovery and layout-aware extraction helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import time
from contextlib import asynccontextmanager
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

SEARXNG_URL: str = os.getenv("SEARXNG_URL", "http://searxng:8080").rstrip("/")
CRAWL4AI_PROXY_URL: str = os.getenv(
    "CRAWL4AI_PROXY_URL", "http://crawl4ai-proxy:8000"
).rstrip("/")
BROWSERLESS_URL: str = os.getenv("BROWSERLESS_URL", "http://browserless:3000").rstrip("/")
BROWSERLESS_WS: str = os.getenv("BROWSERLESS_WS", "ws://browserless:3000").rstrip("/")
ALLOW_PUBLIC_TARGETS: bool = os.getenv("ALLOW_PUBLIC_TARGETS", "false").lower() == "true"
ALLOWED_TARGET_HOSTS = {
    host.strip().lower()
    for host in os.getenv("ALLOWED_TARGET_HOSTS", "crawl4ai,searxng,browserless,crawl4ai-proxy").split(",")
    if host.strip()
}

DEFAULT_SEARCH_RESULTS: int = int(os.getenv("WEB_DISCOVERY_MAX_RESULTS", "5"))
DEFAULT_MAX_TOKENS: int = int(os.getenv("WEB_DISCOVERY_MAX_TOKENS", "1200"))
DEFAULT_MAX_CHARS: int = int(os.getenv("WEB_DISCOVERY_MAX_CHARS", "20000"))
SEARCH_TIMEOUT_S: float = float(os.getenv("WEB_DISCOVERY_SEARCH_TIMEOUT_S", "20"))
PROBE_TIMEOUT_S: float = float(os.getenv("WEB_DISCOVERY_PROBE_TIMEOUT_S", "8"))
PAGE_TIMEOUT_MS: int = int(os.getenv("WEB_DISCOVERY_PAGE_TIMEOUT_MS", "15000"))
SCRIPT_TIMEOUT_MS: int = int(os.getenv("WEB_DISCOVERY_SCRIPT_TIMEOUT_MS", "10000"))
MAX_HTTP_CONCURRENCY: int = max(1, int(os.getenv("WEB_DISCOVERY_HTTP_CONCURRENCY", "2")))
MIN_REQUEST_GAP_S: float = float(os.getenv("WEB_DISCOVERY_MIN_REQUEST_GAP_S", "0.20"))

TRUNCATION_WARNING_TOKEN: str = "[[TRUNCATED: output budget reached]]"

_request_gate = asyncio.Semaphore(MAX_HTTP_CONCURRENCY)
_request_lock = asyncio.Lock()
_last_request_started = 0.0

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\x00", "")
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _normalize_domain(domain: str) -> str:
    cleaned = _clean_string(domain).lower()
    cleaned = cleaned.replace("http://", "").replace("https://", "")
    cleaned = cleaned.strip().strip("/")
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError(f"Invalid domain filter value: {domain!r}")
    return cleaned


def _host_matches(hostname: str, domain: str) -> bool:
    hostname = hostname.lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return hostname == domain or hostname.endswith(f".{domain}")


def _normalize_filters(domain_filters: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in domain_filters or []:
        if not isinstance(item, dict):
            raise ValueError("domain_filters must contain objects with domain and mode.")
        mode = str(item.get("mode", "include")).strip().lower()
        if mode not in {"include", "exclude"}:
            raise ValueError("domain filter mode must be include or exclude.")
        normalized.append({"domain": _normalize_domain(str(item.get("domain", ""))), "mode": mode})
    return normalized


def _build_domain_clause(filters: list[dict[str, str]]) -> str:
    include = [f"site:{f['domain']}" for f in filters if f["mode"] == "include"]
    exclude = [f"-site:{f['domain']}" for f in filters if f["mode"] == "exclude"]
    if include and exclude:
        return " ".join(["(" + " OR ".join(include) + ")"] + exclude)
    if include:
        return "(" + " OR ".join(include) + ")"
    return " ".join(exclude)


def _extract_host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower()


def _target_allowed(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if hostname in ALLOWED_TARGET_HOSTS:
        return True
    if hostname.endswith(".onion"):
        return False
    if not ALLOW_PUBLIC_TARGETS:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)}
    except socket.gaierror:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _allowed_by_filters(url: str, filters: list[dict[str, str]]) -> bool:
    hostname = _extract_host(url)
    if not hostname:
        return False
    include = [item["domain"] for item in filters if item["mode"] == "include"]
    exclude = [item["domain"] for item in filters if item["mode"] == "exclude"]
    if include and not any(_host_matches(hostname, domain) for domain in include):
        return False
    if any(_host_matches(hostname, domain) for domain in exclude):
        return False
    return True


def _token_count(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


def _truncate_layout(text: str, max_tokens: int, max_chars: int) -> tuple[str, bool]:
    if not text:
        return "", False
    if len(text) <= max_chars and _token_count(text) <= max_tokens:
        return text, False
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    if _token_count(text) > max_tokens:
        pieces = _TOKEN_RE.findall(text)
        text = " ".join(pieces[:max_tokens]).rstrip()
    warning = "\n".join(["", TRUNCATION_WARNING_TOKEN]).rstrip()
    return f"{text}{warning}", True


@asynccontextmanager
async def _request_slot():
    global _last_request_started
    async with _request_gate:
        async with _request_lock:
            now = time.monotonic()
            wait_for = max(0.0, MIN_REQUEST_GAP_S - (now - _last_request_started))
            if wait_for:
                await asyncio.sleep(wait_for)
            _last_request_started = time.monotonic()
        yield


async def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with _request_slot():
        async with httpx.AsyncClient(trust_env=False, timeout=SEARCH_TIMEOUT_S) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()


async def _probe_url(url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        async with _request_slot():
            async with httpx.AsyncClient(trust_env=False, timeout=PROBE_TIMEOUT_S) as client:
                response = await client.get(url)
                body = await response.aread()
        return {
            "url": url,
            "status": "reachable" if response.status_code < 500 else "degraded",
            "status_code": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "body_preview": body[:120].decode("utf-8", errors="replace"),
        }
    except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
        return {
            "url": url,
            "status": "unreachable",
            "status_code": None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


async def validate_supporting_endpoints() -> list[dict[str, Any]]:
    return await asyncio.gather(
        _probe_url(f"{SEARXNG_URL}/health"),
        _probe_url(f"{CRAWL4AI_PROXY_URL}/health"),
        _probe_url(f"{BROWSERLESS_URL}/health"),
    )


def _canonical_heading(page_title: str, h1: str, fallback: str) -> str:
    heading = _clean_string(h1) or _clean_string(page_title) or fallback
    return heading


async def _extract_layout_from_page(
    page,
    *,
    fallback_heading: str,
    max_tokens: int,
    max_chars: int,
) -> dict[str, Any]:
    script = """
        ({ maxChars }) => {
            const clean = (value) => {
                if (!value) return "";
                return String(value)
                    .replace(/\\u0000/g, "")
                    .replace(/[\\t\\r ]+/g, " ")
                    .replace(/\\n{3,}/g, "\\n\\n")
                    .replace(/\\s+$/g, "")
                    .trim();
            };
            const visible = (element) => {
                const style = window.getComputedStyle(element);
                return style && style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
            };
            const blocks = [];
            let length = 0;
            let truncated = false;
            const push = (text) => {
                const cleanText = clean(text);
                if (!cleanText) return true;
                const addition = blocks.length ? `\\n\\n${cleanText}` : cleanText;
                if (length + addition.length > maxChars) {
                    truncated = true;
                    return false;
                }
                blocks.push(cleanText);
                length += addition.length;
                return true;
            };
            const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6"));
            if (headings.length) {
                for (const element of headings) {
                    if (!visible(element)) continue;
                    const level = Math.min(6, parseInt(element.tagName.slice(1), 10) || 1);
                    if (!push(`${"#".repeat(level)} ${element.innerText || ""}`)) break;
                }
            } else if (!push(document.title || "")) {
                truncated = true;
            }
            if (!truncated) {
                const semanticNodes = Array.from(document.querySelectorAll("p,li,blockquote,pre"));
                for (const element of semanticNodes) {
                    if (!visible(element)) continue;
                    const text = element.innerText || "";
                    if (!text.trim()) continue;
                    if (element.tagName === "LI") {
                        if (!push(`- ${text}`)) break;
                    } else if (element.tagName === "BLOCKQUOTE") {
                        if (!push(`> ${text}`)) break;
                    } else if (element.tagName === "PRE") {
                        if (!push("```\\n" + text + "\\n```")) break;
                    } else {
                        if (!push(text)) break;
                    }
                }
            }
            if (!truncated) {
                const tables = Array.from(document.querySelectorAll("table"));
                for (const table of tables) {
                    if (!visible(table)) continue;
                    const rows = Array.from(table.rows || []).map((row) => {
                        return Array.from(row.cells || [])
                            .map((cell) => clean(cell.innerText || ""))
                            .filter(Boolean)
                            .join(" | ");
                    }).filter(Boolean);
                    if (!rows.length) continue;
                    if (!push(rows.join("\\n"))) break;
                }
            }
            return {
                title: clean(document.title || ""),
                h1: clean((document.querySelector("h1") || {}).innerText || ""),
                layout: blocks.join("\\n\\n"),
                truncated,
            };
        }
    """
    max_layout_chars = min(max_chars, max(512, max_tokens * 4))
    extracted = await page.evaluate(script, {"maxChars": max_layout_chars})
    layout = _clean_string(extracted.get("layout", ""))
    layout, truncated = _truncate_layout(layout, max_tokens=max_tokens, max_chars=max_layout_chars)
    heading = _canonical_heading(
        extracted.get("title", ""),
        extracted.get("h1", ""),
        fallback_heading,
    )
    if not layout:
        layout = _clean_string(await page.evaluate("() => document.body ? document.body.innerText : document.documentElement.innerText"))  # type: ignore[assignment]
        layout, truncated = _truncate_layout(layout, max_tokens=max_tokens, max_chars=max_layout_chars)
    return {
        "canonical_heading": heading,
        "layout": layout,
        "truncated": bool(extracted.get("truncated")) or truncated,
    }


async def extract_layout_document(
    url: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    if not _target_allowed(url):
        return {
            "status": "error",
            "error_code": "SECURITY_BOUNDARY_VIOLATION",
            "reason": f"Target host is not allowed: {url}",
            "url": url,
        }
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return {
            "status": "error",
            "error_code": "IMPORT_ERROR",
            "reason": f"Playwright import error: {exc}",
            "url": url,
        }

    browser = None
    context = None
    page = None
    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(BROWSERLESS_WS)
            except Exception:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--mute-audio",
                        "--disable-background-networking",
                        "--disable-extensions",
                    ],
                )

            context = await browser.new_context(
                ignore_https_errors=True,
                java_script_enabled=True,
                reduced_motion="reduce",
                viewport={"width": 1365, "height": 1600},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                service_workers="block",
            )

            async def _block_heavy_resources(route):
                resource_type = route.request.resource_type
                if resource_type in {"image", "media", "font"}:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _block_heavy_resources)
            page = await context.new_page()
            page.set_default_timeout(SCRIPT_TIMEOUT_MS)
            page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(5000, PAGE_TIMEOUT_MS))
            except PlaywrightTimeoutError:
                pass

            extracted = await _extract_layout_from_page(
                page,
                fallback_heading=url,
                max_tokens=max_tokens,
                max_chars=max_chars,
            )
            return {
                "status": "success",
                "url": url,
                "canonical_heading": extracted["canonical_heading"],
                "layout": extracted["layout"],
                "http_status": response.status if response else None,
                "truncated": extracted["truncated"],
            }
    except PlaywrightTimeoutError as exc:
        return {
            "status": "error",
            "error_code": "NAVIGATION_TIMEOUT",
            "reason": f"Navigation timeout after {PAGE_TIMEOUT_MS}ms: {exc}",
            "url": url,
        }
    except PlaywrightError as exc:
        msg = str(exc)
        if "ERR_NAME_NOT_RESOLVED" in msg or "ERR_CONNECTION_REFUSED" in msg:
            code = "DNS_OR_CONNECTION_REFUSED"
        elif "403" in msg or "429" in msg or "anti-bot" in msg.lower():
            code = "ANTI_BOT_DETECTED"
        else:
            code = "PLAYWRIGHT_ERROR"
        return {
            "status": "error",
            "error_code": code,
            "reason": msg,
            "url": url,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "INTERNAL_ERROR",
            "reason": f"{type(exc).__name__}: {exc}",
            "url": url,
        }
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass


async def discover_web_layouts(
    query: str,
    *,
    domain_filters: list[dict[str, Any]] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_results: int = DEFAULT_SEARCH_RESULTS,
) -> list[dict[str, Any]] | dict[str, Any]:
    try:
        normalized_filters = _normalize_filters(domain_filters)
    except ValueError as exc:
        return {
            "status": "error",
            "error_code": "INVALID_FILTER",
            "reason": str(exc),
        }

    search_query = _clean_string(query)
    if not search_query:
        return {
            "status": "error",
            "error_code": "INVALID_REQUEST",
            "reason": "query must not be empty.",
        }

    search_payload = search_query
    domain_clause = _build_domain_clause(normalized_filters)
    if domain_clause:
        search_payload = f"{search_query} {domain_clause}".strip()

    params = {
        "q": search_payload,
        "format": "json",
        "safesearch": "1",
        "language": "en",
    }

    try:
        data = await _get_json(f"{SEARXNG_URL}/search", params=params)
    except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
        return {
            "status": "error",
            "error_code": "SEARCH_UPSTREAM_ERROR",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    raw_results = data.get("results", [])
    discovered: list[dict[str, Any]] = []
    for item in raw_results:
        if len(discovered) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url = _clean_string(item.get("url", ""))
        if not url.startswith(("http://", "https://")):
            continue
        if not _allowed_by_filters(url, normalized_filters):
            continue
        heading = _clean_string(item.get("title") or url)
        result = await extract_layout_document(
            url,
            max_tokens=max_tokens,
            max_chars=max_chars,
        )
        if result.get("status") != "success":
            logger.warning(
                "[WEB-DISCOVERY] Skipping %s due to %s: %s",
                url,
                result.get("error_code"),
                result.get("reason"),
            )
            continue
        discovered.append(
            {
                "uri": url,
                "canonical_heading": result.get("canonical_heading") or heading,
                "layout": result.get("layout", ""),
            }
        )

    return discovered
