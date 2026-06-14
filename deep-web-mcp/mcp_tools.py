"""
mcp_tools.py — FastMCP instance, Pydantic schemas, and @mcp.tool() definitions.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from extraction import crawl4ai_extract, new_task
from policy import (
    RESEARCH_MAX_HOPS,
    RESEARCH_MAX_ITERATIONS,
    RESEARCH_MAX_SOURCES,
    RESEARCH_TOTAL_BUDGET_S,
)
from research import research_web as run_research_web
from web_discovery import (
    DEFAULT_MAX_CHARS as DISCOVERY_DEFAULT_MAX_CHARS,
    DEFAULT_SEARCH_RESULTS as DISCOVERY_DEFAULT_MAX_RESULTS,
    DEFAULT_MAX_TOKENS as DISCOVERY_DEFAULT_MAX_TOKENS,
    SEARXNG_URL,
    discover_web_layouts as run_web_discovery,
)

# Per-request SearXNG timeout (distinct from the web-discovery probe timeout).
SEARXNG_TIMEOUT_S: float = float(os.getenv("SEARXNG_TIMEOUT_S", "5"))

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "DeepWebOrchestrator",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ---------------------------------------------------------------------------
# Pydantic schemas (shared with api.py)
# ---------------------------------------------------------------------------
class DomainFilterInput(BaseModel):
    domain: str = Field(..., min_length=1)
    mode: Literal["include", "exclude"] = Field("include")


class WebDiscoveryInput(BaseModel):
    query:          str  = Field(..., min_length=1)
    domain_filters: list[DomainFilterInput] = Field(default_factory=list)
    max_tokens:     int  = Field(default=DISCOVERY_DEFAULT_MAX_TOKENS, ge=32,  le=20_000)
    max_chars:      int  = Field(default=DISCOVERY_DEFAULT_MAX_CHARS,  ge=512, le=100_000)
    max_results:    int  = Field(default=DISCOVERY_DEFAULT_MAX_RESULTS, ge=1,  le=10)


class ResearchInput(BaseModel):
    query:          str  = Field(..., min_length=1, max_length=500)
    strategy: Literal["auto", "general", "deep"] = "auto"
    domain_filters: list[DomainFilterInput] = Field(default_factory=list)
    max_iterations: int = Field(default=RESEARCH_MAX_ITERATIONS, ge=1, le=RESEARCH_MAX_ITERATIONS)
    max_sources: int = Field(default=RESEARCH_MAX_SOURCES, ge=1, le=RESEARCH_MAX_SOURCES)
    max_hops: int = Field(default=RESEARCH_MAX_HOPS, ge=1, le=RESEARCH_MAX_HOPS)
    total_budget_s: float = Field(default=RESEARCH_TOTAL_BUDGET_S, ge=10.0, le=900.0)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def fetch_deep_web_data(
    url: str, session_required: bool = False, js_script: str = None
) -> str:
    """
    Extract and sanitize content from an allowed internal service via Crawl4AI.

    Parameters
    ----------
    url : str
        Allowed internal URL whose host is in ALLOWED_TARGET_HOSTS.
    session_required : bool
        When True, loads JWT / cookies from the CredentialVault before crawling.
    js_script : str | None
        Optional JavaScript to evaluate after page load.
    """
    from urllib.parse import urlparse
    task_id = new_task(url)
    result = await crawl4ai_extract(
        url=url,
        thread_id=(urlparse(url).hostname or "default"),
        session_required=session_required,
        js_eval=js_script,
        task_id=task_id,
    )
    return json.dumps(result)


@mcp.tool()
async def search_deep_web_database(
    target_database: str, search_query: str, session_required: bool = False
) -> str:
    """Search specific internal SearXNG engines.

    Parameters
    ----------
    target_database : str
        SearXNG engine identifier (e.g. ``bing``, ``google``, ``duckduckgo``).
    search_query : str
        Query string to submit.
    session_required : bool
        Reserved for authenticated SearXNG instances.
    """
    import httpx
    engine = target_database.strip().lower() or "bing"
    if not re.fullmatch(r"[a-z0-9 _,-]{1,100}", engine):
        return json.dumps({"status": "error", "message": "Invalid SearXNG engine identifier."})
    query = search_query.strip()
    if not query:
        return json.dumps({"status": "error", "message": "search_query must not be empty."})

    params = {"q": query, "engines": engine, "format": "json", "safesearch": "1"}
    headers = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
    async with httpx.AsyncClient(headers=headers) as client:
        try:
            resp = await client.get(
                f"{SEARXNG_URL}/search", params=params, timeout=SEARXNG_TIMEOUT_S
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                {
                    "title":          item.get("title") or item.get("url") or "Untitled",
                    "url":            item.get("url", ""),
                    "content":        item.get("content", ""),
                    "engine":         item.get("engine") or engine,
                    "score":          item.get("score"),
                    "published_date": item.get("publishedDate"),
                }
                for item in data.get("results", [])[:10]
            ]
            query_terms = set(re.findall(r"[a-z0-9]+", query.lower()))

            def relevance(item: dict) -> float:
                haystack = f"{item['title']} {item['url']} {item['content']}".lower()
                score = float(item.get("score") or 0)
                score += sum(term in haystack for term in query_terms)
                if {"github", "repository", "repo"} & query_terms and "github.com/" in item["url"].lower():
                    score += 5
                return score

            results.sort(key=relevance, reverse=True)
            results = results[:5]
            return json.dumps({
                "status":              "success",
                "source":              "live",
                "query":               query,
                "engine":              engine,
                "route":               "searxng_internal",
                "result_count":        len(results),
                "unresponsive_engines": data.get("unresponsive_engines", []),
                "best_match":          results[0] if results else None,
                "results":             results,
            })
        except Exception as exc:
            return json.dumps({"status": "error", "query": query, "engine": engine, "message": str(exc)})


@mcp.tool()
async def discover_web_layouts(
    query: str,
    domain_filters: list[dict[str, Any]] | None = None,
    max_tokens: int = DISCOVERY_DEFAULT_MAX_TOKENS,
    max_chars:  int = DISCOVERY_DEFAULT_MAX_CHARS,
    max_results: int = DISCOVERY_DEFAULT_MAX_RESULTS,
) -> str:
    """Return a JSON array of discovered URI, heading, and layout items."""
    try:
        result = await run_web_discovery(
            query,
            domain_filters=domain_filters or [],
            max_tokens=max_tokens,
            max_chars=max_chars,
            max_results=max_results,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.exception("[DISCOVERY] MCP tool failed.")
        return json.dumps(
            {"status": "error", "error_code": "DISCOVERY_FAILED",
             "reason": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )


@mcp.tool()
async def research_web(
    query: str,
    strategy: Literal["auto", "general", "deep"] = "auto",
    domain_filters: list[dict[str, Any]] | None = None,
    max_iterations: int = RESEARCH_MAX_ITERATIONS,
    max_sources: int = RESEARCH_MAX_SOURCES,
    max_hops: int = RESEARCH_MAX_HOPS,
    total_budget_s: float = RESEARCH_TOTAL_BUDGET_S,
) -> str:
    """Run bounded multi-hop web research with verified links and sufficiency evaluation."""
    try:
        result = await run_research_web(
            query=query,
            strategy=strategy,
            domain_filters=domain_filters or [],
            max_iterations=max_iterations,
            max_sources=max_sources,
            max_hops=max_hops,
            total_budget_s=total_budget_s,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.exception("[RESEARCH] MCP tool failed.")
        return json.dumps(
            {"status": "error", "error_code": "RESEARCH_FAILED",
             "reason": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )
