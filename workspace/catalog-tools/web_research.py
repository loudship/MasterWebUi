"""
title: Web Research Router
author: Local Operations
version: 1.0.0
description: Deterministic General and Deep Research through the local Deep Web MCP service.
requirements: httpx
"""

import json
from typing import Literal

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        research_url: str = Field(default="http://deep-web-mcp:8000/research")
        timeout_seconds: int = Field(default=180, ge=15, le=600)

    def __init__(self):
        self.valves = self.Valves()

    async def research_web(
        self,
        query: str,
        strategy: Literal["auto", "general", "deep"] = "auto",
        domain_filters: list[dict] | None = None,
        max_iterations: int = 3,
        max_sources: int = 8,
    ) -> str:
        """Run bounded live web research and return verified links plus a Markdown report."""
        payload = {
            "query": query,
            "strategy": strategy,
            "domain_filters": domain_filters or [],
            "max_iterations": max_iterations,
            "max_sources": max_sources,
        }
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=self.valves.timeout_seconds) as client:
                response = await client.post(self.valves.research_url, json=payload)
                response.raise_for_status()
                return json.dumps(response.json(), ensure_ascii=False)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            return json.dumps(
                {"status": "error", "error_code": "RESEARCH_SERVICE_ERROR", "reason": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
