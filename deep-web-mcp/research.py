"""Deterministic, bounded web-research orchestration."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from web_discovery import (
    SEARCH_TIMEOUT_S,
    SEARXNG_URL,
    _allowed_by_filters,
    _build_domain_clause,
    _clean_string,
    _normalize_filters,
    extract_layout_document,
)

ResearchStrategy = Literal["auto", "general", "deep"]
_DEEP_HINTS = re.compile(
    r"\b(deep research|investigate|comprehensive|exhaustive|compare|landscape|report|evidence|why)\b",
    re.I,
)


def choose_strategy(query: str, strategy: ResearchStrategy) -> Literal["general", "deep"]:
    if strategy in {"general", "deep"}:
        return strategy
    return "deep" if _DEEP_HINTS.search(query) else "general"


def _markdown_link(title: str, url: str) -> str:
    return f"[{title.replace('[', '').replace(']', '')}]({url})"


async def _search(query: str, filters: list[dict[str, str]], limit: int) -> list[dict[str, Any]]:
    clause = _build_domain_clause(filters)
    search_query = f"{query} {clause}".strip() if clause else query
    params = {"q": search_query, "format": "json", "safesearch": "1", "language": "en"}
    async with httpx.AsyncClient(trust_env=False, timeout=SEARCH_TIMEOUT_S) as client:
        response = await client.get(f"{SEARXNG_URL}/search", params=params)
        response.raise_for_status()
        payload = response.json()
    results = []
    for item in payload.get("results", []):
        url = _clean_string(item.get("url"))
        if not url.startswith(("http://", "https://")) or not _allowed_by_filters(url, filters):
            continue
        results.append(
            {
                "title": _clean_string(item.get("title") or url),
                "url": url,
                "domain": (urlparse(url).hostname or "").lower(),
                "summary": _clean_string(item.get("content")),
                "engine": item.get("engine"),
                "published_date": item.get("publishedDate"),
            }
        )
        if len(results) >= limit:
            break
    return results


async def _validate_link(item: dict[str, Any], gate: asyncio.Semaphore) -> dict[str, Any]:
    async with gate:
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(8.0),
                follow_redirects=True,
            ) as client:
                response = await client.get(item["url"], headers={"Range": "bytes=0-1024"})
            active = response.status_code < 400 or response.status_code in {401, 403, 405, 429}
            item["link_status"] = "active" if active else "failed"
            item["verified"] = active
            item["http_status"] = response.status_code
            item["verified_url"] = str(response.url)
        except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
            item["link_status"] = "unavailable"
            item["verified"] = False
            item["verification_error"] = f"{type(exc).__name__}: {exc}"
        return item


async def research_web(
    query: str,
    strategy: ResearchStrategy = "auto",
    domain_filters: list[dict[str, Any]] | None = None,
    max_iterations: int = 3,
    max_sources: int = 8,
) -> dict[str, Any]:
    query = _clean_string(query)
    if not query:
        return {"status": "error", "error_code": "INVALID_REQUEST", "reason": "query must not be empty."}
    filters = _normalize_filters(domain_filters)
    selected = choose_strategy(query, strategy)
    max_iterations = max(1, min(int(max_iterations), 3))
    max_sources = max(1, min(int(max_sources), 8))
    gate = asyncio.Semaphore(4)
    trace: list[dict[str, Any]] = []

    initial = await _search(query, filters, max_sources)
    sources = await asyncio.gather(*(_validate_link(item, gate) for item in initial))
    trace.append({"iteration": 1, "query": query, "results": len(sources), "purpose": "initial search"})

    gap_queries: list[str] = []
    if selected == "deep":
        gap_queries = [f"{query} official documentation", f"{query} recent developments"]
        for iteration, gap_query in enumerate(gap_queries[: max_iterations - 1], start=2):
            extra = await _search(gap_query, filters, max_sources)
            known = {item["url"] for item in sources}
            extra = [item for item in extra if item["url"] not in known]
            verified = await asyncio.gather(*(_validate_link(item, gate) for item in extra))
            sources.extend(verified)
            trace.append(
                {"iteration": iteration, "query": gap_query, "results": len(verified), "purpose": "coverage gap"}
            )
            if len(sources) >= max_sources:
                break

    sources = sources[:max_sources]
    if selected == "deep":
        extract_gate = asyncio.Semaphore(3)

        async def extract(item: dict[str, Any]) -> dict[str, Any]:
            if item.get("link_status") != "active":
                item["extraction_status"] = "skipped"
                return item
            async with extract_gate:
                result = await extract_layout_document(item.get("verified_url") or item["url"], max_tokens=900, max_chars=7000)
            item["extraction_status"] = result.get("status", "error")
            item["extracted_text"] = result.get("layout", "")
            item["extraction_error"] = result.get("reason")
            return item

        sources = await asyncio.gather(*(extract(item) for item in sources))

    lines = [f"# Web Research: {query}", "", f"Strategy: **{selected}**", "", "## Sources"]
    for index, source in enumerate(sources, start=1):
        link = _markdown_link(source["title"], source.get("verified_url") or source["url"])
        summary = source.get("extracted_text") or source.get("summary") or "No extractable summary."
        summary = " ".join(summary.split())[:800]
        lines.extend(["", f"{index}. {link}", f"   - Domain: `{source['domain']}`", f"   - Link: {source.get('link_status')}", f"   - Evidence: {summary}"])
    lines.extend(["", "## Search Trace"])
    lines.extend(f"- Iteration {item['iteration']}: `{item['query']}` ({item['results']} results)" for item in trace)

    return {
        "status": "success",
        "query": query,
        "requested_strategy": strategy,
        "strategy": selected,
        "strategy_trace": trace,
        "gap_queries": gap_queries[: max(0, max_iterations - 1)],
        "sources": sources,
        "citations": [
            {"index": index, "title": item["title"], "url": item.get("verified_url") or item["url"]}
            for index, item in enumerate(sources, start=1)
        ],
        "markdown_report": "\n".join(lines),
    }
