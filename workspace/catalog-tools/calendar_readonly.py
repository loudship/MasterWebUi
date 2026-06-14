"""
title: Calendar Read-Only Streamable HTTP
version: 2.0.0
description: Read-only Calendar MCP queries over bounded Streamable HTTP.
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        server_url: str = Field(default="http://calendar-mcp:8000/mcp")
        jwt_token: str = Field(
            default="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0IiwiaXNzIjoiY2FsZW5kYXItbWNwLWlzc3VlciIsImF1ZCI6ImNhbGVuZGFyLW1jcC1hcGkifQ.4h3c-sHhMANe9ipqBnNScBrjHk2wZUh3U53VlkZpc_0"
        )
        timeout_seconds: float = Field(default=30, ge=5, le=120)

    def __init__(self):
        self.valves = self.Valves()
        self._gate = asyncio.Semaphore(4)

    async def _call_mcp(self, tool_name: str, arguments: dict, user_id: str) -> str:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        url = self.valves.server_url.strip()
        if not url.startswith(("http://", "https://")):
            return json.dumps({"status": "error", "error_code": "INVALID_URL", "reason": "Calendar MCP URL must use HTTP or HTTPS."})
        headers = {"X-User-ID": user_id}
        if self.valves.jwt_token:
            headers["Authorization"] = f"Bearer {self.valves.jwt_token}"
        try:
            async with self._gate:
                async with asyncio.timeout(self.valves.timeout_seconds):
                    async with streamablehttp_client(url, headers=headers, timeout=self.valves.timeout_seconds) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments)
            text = "\n".join(getattr(item, "text", "") for item in result.content).strip()
            return text or json.dumps({"status": "success", "data": []})
        except (TimeoutError, OSError, ValueError, ExceptionGroup) as exc:
            return json.dumps({"status": "error", "error_code": "CALENDAR_MCP_FAILURE", "reason": f"{type(exc).__name__}: {exc}"})

    async def get_events_today(self, __user__: dict | None = None) -> str:
        """Get today's events without changing calendar state."""
        return await self._call_mcp("get_events_today", {}, str((__user__ or {}).get("id") or "workspace-audit"))

    async def get_events_this_week(self, __user__: dict | None = None) -> str:
        """Get this week's events without changing calendar state."""
        return await self._call_mcp("get_events_this_week", {}, str((__user__ or {}).get("id") or "workspace-audit"))

    async def list_calendars(self, __user__: dict | None = None) -> str:
        """List calendars without changing calendar state."""
        return await self._call_mcp("list_calendars", {}, str((__user__ or {}).get("id") or "workspace-audit"))
