"""
title: Web Research
author: Local Operations
version: 4.0.0
description: |
  Thin Open WebUI Tool that delegates all research orchestration — multi-hop
  execution, sufficiency evaluation, gap queries, budget management, and report
  rendering — to the Deep Web MCP /research endpoint.

  Egress policy: no external hosts contacted here. All search and link validation
  happens inside deep-web-mcp (the controlled egress perimeter).

requirements: httpx
"""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        research_url: str = Field(
            default="http://deep-web-mcp:8000/research",
            description="Deep Web MCP /research endpoint.",
        )
        request_timeout_seconds: int = Field(
            default=120,
            ge=30,
            le=900,
            description="Transport timeout; research policy is owned by Deep Web MCP.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def research_web(
        self,
        query: str,
        strategy: Literal["auto", "general", "deep"] = "auto",
        domain_filters: list[dict] | None = None,
        max_sources: int = 8,
    ) -> str:
        """
        Run bounded, agentic multi-hop web research.

        Multi-hop execution (up to 4 hops), sufficiency evaluation, gap-query
        generation, budget management, and Markdown report rendering all happen
        inside the Deep Web MCP service.  This tool is a thin HTTP caller.

        Parameters
        ----------
        query : str
            Research question.
        strategy : "auto" | "general" | "deep"
            "auto" promotes to "deep" for investigation-style queries.
        domain_filters : list[dict] | None
            Optional include/exclude domain filters, e.g.
            ``[{"domain": "reddit.com", "mode": "exclude"}]``.
        max_sources : int
            Maximum sources to include in the final report (1–8).
        """
        query = query.strip()
        if not query:
            return json.dumps(
                {"status": "error", "error_code": "INVALID_REQUEST", "reason": "query must not be empty."},
                ensure_ascii=False,
            )

        payload = {
            "query":          query,
            "strategy":       strategy,
            "domain_filters": domain_filters or [],
            "max_sources":    max(1, min(int(max_sources), 8)),
        }

        try:
            async with httpx.AsyncClient(
                trust_env=False, timeout=float(self.valves.request_timeout_seconds)
            ) as client:
                response = await client.post(self.valves.research_url, json=payload)
                response.raise_for_status()
                return json.dumps(response.json(), ensure_ascii=False)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return json.dumps(
                {
                    "status":     "error",
                    "error_code": "RESEARCH_SERVICE_ERROR",
                    "reason":     f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
