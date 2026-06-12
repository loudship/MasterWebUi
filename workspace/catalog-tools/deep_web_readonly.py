"""
title: Deep Web MCP Search and Extract
author: Local Operations
version: 2.0.0
description: Read-only search and extraction through the local Deep Web MCP service.
requirements: mcp
"""

import asyncio
import json
from pydantic import BaseModel, Field


def _text(result) -> str:
    return "\n".join(str(getattr(block, "text", "")) for block in result.content if getattr(block, "text", None))


def _error(code: str, exc: Exception | str) -> str:
    return json.dumps({"status": "error", "error_code": code, "reason": str(exc)}, ensure_ascii=False)


class Tools:
    class Valves(BaseModel):
        server_url: str = Field(
            default="http://deep-web-mcp:8000/sse",
            description="Local Deep Web MCP SSE endpoint.",
        )
        timeout_seconds: int = Field(default=180, ge=10, le=600)

    def __init__(self):
        self.valves = self.Valves()

    async def extract_url(self, url: str) -> str:
        """Extract a public URL without sessions or arbitrary JavaScript."""
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        try:
            async with asyncio.timeout(self.valves.timeout_seconds):
                async with sse_client(self.valves.server_url) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        text = _text(
                            await session.call_tool(
                                "fetch_deep_web_data",
                                arguments={"url": url.strip(), "session_required": False, "js_script": ""},
                            )
                        )
                        return text or _error("EMPTY_RESPONSE", "Extraction returned no content.")
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            return _error("EXTRACTION_FAILED", exc)

    async def search_database(self, target_database: str, search_query: str) -> str:
        """Search a configured database without authenticated sessions."""
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        try:
            async with asyncio.timeout(self.valves.timeout_seconds):
                async with sse_client(self.valves.server_url) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        text = _text(
                            await session.call_tool(
                                "search_deep_web_database",
                                arguments={
                                    "target_database": target_database.strip() or "bing",
                                    "search_query": search_query.strip(),
                                    "session_required": False,
                                },
                            )
                        )
                        return text or _error("EMPTY_RESPONSE", "Search returned no content.")
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            return _error("SEARCH_FAILED", exc)

    async def discover_layouts(
        self,
        query: str,
        domain_filters: list[dict] | None = None,
        max_tokens: int = 1200,
        max_chars: int = 20_000,
        max_results: int = 5,
    ) -> str:
        """Discover pages and return strict JSON items with headings and layouts."""
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        try:
            async with asyncio.timeout(self.valves.timeout_seconds):
                async with sse_client(self.valves.server_url) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        text = _text(await session.call_tool(
                        "discover_web_layouts",
                        arguments={
                            "query": query,
                            "domain_filters": domain_filters or [],
                            "max_tokens": max_tokens,
                            "max_chars": max_chars,
                            "max_results": max_results,
                        },
                        ))
                        return text or _error("EMPTY_RESPONSE", "Discovery returned no content.")
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            return _error("DISCOVERY_FAILED", exc)
