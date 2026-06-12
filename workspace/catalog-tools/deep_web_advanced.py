"""
title: Deep Web MCP Advanced Session and JavaScript
author: Local Operations
version: 1.0.0
description: Operator-only Deep Web MCP access for session-backed extraction and explicit JavaScript.
requirements: mcp
"""

import asyncio
import json
from pydantic import BaseModel, Field

CONFIRMATION = "CONFIRM_ADVANCED_DEEP_WEB"


def _text(result) -> str:
    return "\n".join(str(getattr(block, "text", "")) for block in result.content if getattr(block, "text", None))


class Tools:
    class Valves(BaseModel):
        server_url: str = Field(default="http://deep-web-mcp:8000/sse")
        timeout_seconds: int = Field(default=180, ge=10, le=600)

    def __init__(self):
        self.valves = self.Valves()

    async def advanced_extract(
        self,
        url: str,
        confirmation: str,
        session_required: bool = False,
        js_script: str = "",
    ) -> str:
        """Run advanced extraction only after an operator supplies the exact confirmation phrase."""
        if confirmation != CONFIRMATION:
            return f"Blocked. Supply the exact operator confirmation phrase: {CONFIRMATION}"

        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        try:
            async with asyncio.timeout(self.valves.timeout_seconds):
                async with sse_client(self.valves.server_url) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        text = _text(await session.call_tool(
                        "fetch_deep_web_data",
                        arguments={
                            "url": url,
                            "session_required": session_required,
                            "js_script": js_script,
                        },
                        ))
                        return text or json.dumps({"status": "error", "error_code": "EMPTY_RESPONSE"})
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            return json.dumps({"status": "error", "error_code": "ADVANCED_EXTRACTION_FAILED", "reason": str(exc)})
