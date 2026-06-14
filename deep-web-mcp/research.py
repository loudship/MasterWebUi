"""Deterministic, bounded web-research orchestration with optional multi-hop."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from policy import (
    RESEARCH_MAX_HOPS,
    RESEARCH_MAX_ITERATIONS,
    RESEARCH_MAX_SOURCES,
    RESEARCH_MIN_ACTIVE_SOURCES as _MIN_ACTIVE_SOURCES,
    RESEARCH_MIN_EVIDENCE_CHARS as _MIN_EVIDENCE_CHARS,
    RESEARCH_TOTAL_BUDGET_S,
)
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

# Gap sub-query templates.  {year} is resolved at call time so patterns stay fresh.
_GAP_PATTERNS = [
    "{query} official documentation",
    "{query} recent developments {year}",
    "{query} technical deep dive analysis",
]


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
                "title":          _clean_string(item.get("title") or url),
                "url":            url,
                "domain":         (urlparse(url).hostname or "").lower(),
                "summary":        _clean_string(item.get("content")),
                "engine":         item.get("engine"),
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
            item["link_status"]        = "active" if active else "failed"
            item["verified"]           = active
            item["http_status"]        = response.status_code
            item["verified_url"]       = str(response.url)
        except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
            item["link_status"]        = "unavailable"
            item["verified"]           = False
            item["verification_error"] = f"{type(exc).__name__}: {exc}"
        return item


def _is_sufficient(sources: list[dict[str, Any]]) -> bool:
    """Return True when gathered sources satisfy the query intent.

    Both conditions must hold:
    1. At least _MIN_ACTIVE_SOURCES verified-active links.
    2. Total evidence text >= _MIN_EVIDENCE_CHARS characters.
    """
    active = [s for s in sources if s.get("verified") or s.get("link_status") == "active"]
    if len(active) < _MIN_ACTIVE_SOURCES:
        return False
    evidence = sum(
        len(s.get("extracted_text") or s.get("summary") or "")
        for s in active
    )
    return evidence >= _MIN_EVIDENCE_CHARS


def _build_report(
    query: str,
    selected: str,
    sources: list[dict[str, Any]],
    trace: list[dict[str, Any]],
) -> str:
    lines = [f"# Web Research: {query}", "", f"Strategy: **{selected}**", "", "## Sources"]
    seen: set[str] = set()
    for idx, source in enumerate(sources, start=1):
        url = source.get("verified_url") or source["url"]
        if url in seen:
            continue
        seen.add(url)
        link    = _markdown_link(source["title"], url)
        summary = source.get("extracted_text") or source.get("summary") or "No extractable summary."
        summary = " ".join(summary.split())[:800]
        lines += [
            "", f"{idx}. {link}",
            f"   - Domain: `{source['domain']}`  |  Link: {source.get('link_status')}",
            f"   - Evidence: {summary}",
        ]
    lines += ["", "## Search Trace"]
    for item in trace:
        key = "iteration" if "iteration" in item else "hop"
        lines.append(
            f"- {key.capitalize()} {item[key]}: `{item['query']}` ({item['results']} results)"
        )
    return "\n".join(lines)


async def research_web(
    query: str,
    strategy: ResearchStrategy = "auto",
    domain_filters: list[dict[str, Any]] | None = None,
    max_iterations: int = RESEARCH_MAX_ITERATIONS,
    max_sources: int = RESEARCH_MAX_SOURCES,
    max_hops: int = 1,
    total_budget_s: float | None = None,
) -> dict[str, Any]:
    """Run deterministic, bounded web research.

    Parameters
    ----------
    max_hops : int
        When > 1, enables the outer sufficiency-gated hop loop with gap-query
        expansion and a time budget.  When 1 (default), performs a single
        search + optional deep-iteration pass (original behaviour).
    total_budget_s : float | None
        Hard wall-clock deadline for the multi-hop loop.  Ignored when
        max_hops == 1.
    """
    query = _clean_string(query)
    if not query:
        return {"status": "error", "error_code": "INVALID_REQUEST", "reason": "query must not be empty."}

    filters     = _normalize_filters(domain_filters)
    selected    = choose_strategy(query, strategy)
    max_iters   = max(1, min(int(max_iterations), RESEARCH_MAX_ITERATIONS))
    max_src     = max(1, min(int(max_sources), RESEARCH_MAX_SOURCES))
    max_hops    = max(1, min(int(max_hops), RESEARCH_MAX_HOPS))
    gate        = asyncio.Semaphore(4)

    # ------------------------------------------------------------------
    # Multi-hop outer loop (enabled when max_hops > 1)
    # ------------------------------------------------------------------
    if max_hops > 1:
        deadline        = time.monotonic() + (total_budget_s or RESEARCH_TOTAL_BUDGET_S)
        all_sources:    list[dict[str, Any]] = []
        seen_urls:      set[str]             = set()
        trace:          list[dict[str, Any]] = []
        ceiling_hit     = False
        budget_exhausted = False
        current_year    = date.today().year

        for hop in range(1, max_hops + 1):
            hop_start = time.monotonic()
            remaining = deadline - hop_start
            if remaining < 2.0:
                budget_exhausted = True
                break

            hop_query = query if hop == 1 else \
                _GAP_PATTERNS[(hop - 2) % len(_GAP_PATTERNS)].format(
                    query=query, year=current_year
                )

            try:
                raw = await _search(hop_query, filters, max_src)
            except Exception as exc:
                trace.append({
                    "hop": hop, "query": hop_query, "results": 0,
                    "sufficient": False,
                    "elapsed_ms": int((time.monotonic() - hop_start) * 1000),
                    "error": str(exc),
                })
                if hop == 1:
                    return {
                        "status": "error", "error_code": "SEARCH_FAILED",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                break

            fresh = [s for s in raw if s["url"] not in seen_urls]
            validated = list(await asyncio.gather(*(_validate_link(s, gate) for s in fresh)))
            for s in validated:
                seen_urls.add(s["url"])
                all_sources.append(s)

            if selected == "deep":
                extract_gate = asyncio.Semaphore(3)

                async def _extract(item: dict[str, Any]) -> dict[str, Any]:
                    if item.get("link_status") != "active":
                        item["extraction_status"] = "skipped"
                        return item
                    async with extract_gate:
                        result = await extract_layout_document(
                            item.get("verified_url") or item["url"],
                            max_tokens=900, max_chars=7000,
                        )
                    item["extraction_status"] = result.get("status", "error")
                    item["extracted_text"]    = result.get("layout", "")
                    item["extraction_error"]  = result.get("reason")
                    return item

                all_sources = list(await asyncio.gather(*(_extract(s) for s in all_sources)))

            sufficient = _is_sufficient(all_sources)
            trace.append({
                "hop":        hop,
                "query":      hop_query,
                "results":    len(validated),
                "sufficient": sufficient,
                "elapsed_ms": int((time.monotonic() - hop_start) * 1000),
            })

            if sufficient:
                break
            if hop == max_hops:
                ceiling_hit = True

        trimmed = all_sources[:max_src]
        limit_note = ""
        if ceiling_hit:
            limit_note = " | hop ceiling reached"
        elif budget_exhausted:
            limit_note = " | time budget exhausted"
        lines = [
            f"# Web Research Report: {query}", "",
            f"**Strategy**: {selected}  |  **Hops**: {len(trace)}{limit_note}", "",
            "## Sources",
        ]
        seen: set[str] = set()
        for idx, source in enumerate(trimmed, start=1):
            url = source.get("verified_url") or source["url"]
            if url in seen:
                continue
            seen.add(url)
            link    = _markdown_link(source["title"], url)
            summary = source.get("extracted_text") or source.get("summary") or "No extractable summary."
            summary = " ".join(summary.split())[:800]
            lines += [
                "", f"{idx}. {link}",
                f"   - Domain: `{source['domain']}`  |  Link: {source.get('link_status')}",
                f"   - Evidence: {summary}",
            ]
        lines += ["", "## Hop Trace"]
        for hop_rec in trace:
            lines.append(
                f"- **Hop {hop_rec['hop']}**: `{hop_rec['query']}` — "
                f"{hop_rec['results']} sources  |  "
                f"Sufficient: {'✓' if hop_rec['sufficient'] else '✗'}  |  "
                f"Elapsed: {hop_rec['elapsed_ms']}ms"
            )

        return {
            "status":           "success",
            "query":            query,
            "requested_strategy": strategy,
            "strategy":         selected,
            "hops_executed":    len(trace),
            "ceiling_hit":      ceiling_hit,
            "budget_exhausted": budget_exhausted,
            "sources":          trimmed,
            "citations": [
                {"index": i, "title": s["title"],
                 "url": s.get("verified_url") or s["url"]}
                for i, s in enumerate(trimmed, start=1)
            ],
            "markdown_report":  "\n".join(lines),
            "hop_trace":        trace,
        }

    # ------------------------------------------------------------------
    # Single-call path (max_hops == 1, original behaviour)
    # ------------------------------------------------------------------
    trace_single: list[dict[str, Any]] = []

    initial = await _search(query, filters, max_src)
    sources = list(await asyncio.gather(*(_validate_link(item, gate) for item in initial)))
    trace_single.append({"iteration": 1, "query": query, "results": len(sources), "purpose": "initial search"})

    gap_queries: list[str] = []
    if selected == "deep":
        gap_queries = [f"{query} official documentation", f"{query} recent developments"]
        for iteration, gap_query in enumerate(gap_queries[: max_iters - 1], start=2):
            extra = await _search(gap_query, filters, max_src)
            known = {item["url"] for item in sources}
            extra = [item for item in extra if item["url"] not in known]
            verified = list(await asyncio.gather(*(_validate_link(item, gate) for item in extra)))
            sources.extend(verified)
            trace_single.append(
                {"iteration": iteration, "query": gap_query, "results": len(verified), "purpose": "coverage gap"}
            )
            if len(sources) >= max_src:
                break

    sources = sources[:max_src]
    if selected == "deep":
        extract_gate = asyncio.Semaphore(3)

        async def _extract_single(item: dict[str, Any]) -> dict[str, Any]:
            if item.get("link_status") != "active":
                item["extraction_status"] = "skipped"
                return item
            async with extract_gate:
                result = await extract_layout_document(
                    item.get("verified_url") or item["url"], max_tokens=900, max_chars=7000
                )
            item["extraction_status"] = result.get("status", "error")
            item["extracted_text"]    = result.get("layout", "")
            item["extraction_error"]  = result.get("reason")
            return item

        sources = list(await asyncio.gather(*(_extract_single(item) for item in sources)))

    return {
        "status":             "success",
        "query":              query,
        "requested_strategy": strategy,
        "strategy":           selected,
        "strategy_trace":     trace_single,
        "gap_queries":        gap_queries[: max(0, max_iters - 1)],
        "sources":            sources,
        "citations": [
            {"index": idx, "title": item["title"], "url": item.get("verified_url") or item["url"]}
            for idx, item in enumerate(sources, start=1)
        ],
        "markdown_report":    _build_report(query, selected, sources, trace_single),
    }
