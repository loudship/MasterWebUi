"""
title: Deep Web MCP Search and Extract
author: Local Operations
version: 2.0.0
description: Read-only search and extraction through the local Deep Web MCP service.
requirements: mcp
"""

from pydantic import BaseModel, Field


def _text(result) -> str:
    return "\n".join(str(getattr(block, "text", "")) for block in result.content if getattr(block, "text", None))


class Tools:
    class Valves(BaseModel):
        server_url: str = Field(
            default="http://deep-web-mcp:8000/sse",
            description="Local Deep Web MCP SSE endpoint.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def extract_url(self, url: str) -> str:
        """Extract a public URL without sessions or arbitrary JavaScript."""
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(self.valves.server_url) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                return _text(
                    await session.call_tool(
                        "fetch_deep_web_data",
                        arguments={"url": url, "session_required": False, "js_script": ""},
                    )
                )

    async def search_database(self, target_database: str, search_query: str) -> str:
        """Search a configured database without authenticated sessions."""
        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(self.valves.server_url) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                return _text(
                    await session.call_tool(
                        "search_deep_web_database",
                        arguments={
                            "target_database": target_database,
                            "search_query": search_query,
                            "session_required": False,
                        },
                    )
                )
