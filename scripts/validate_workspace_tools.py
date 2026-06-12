#!/usr/bin/env python3
"""Load every live Open WebUI tool and run bounded, non-destructive probes."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from open_webui.utils.plugin import load_tool_module_by_id

PROBES: dict[str, tuple[str, dict[str, Any]]] = {
    "run_code_py": ("run_python_code", {"python_code": "print(2 + 2)"}),
    "deep_web_ecosystem_tools": ("search_database", {"target_database": "web", "search_query": "Open WebUI"}),
    "calendar_ecosystem_tools": ("get_events_today", {}),
    "home_assit": ("query_entities", {"mode": "domain", "domain": "light"}),
    "youtube_transcript_provider": ("get_youtube_transcript", {"url": "invalid"}),
    "swarm_controls": ("get_swarm_status", {}),
    "mcp_app_bridge": ("list_mcp_tools", {}),
    "inline_visualizer": ("visualize", {"data": "audit probe", "chart_type": "text"}),
    "deep_web_advanced_tools": ("advanced_extract", {"url": "https://example.com", "confirmation": ""}),
    "web_research": ("research_web", {"query": "Open WebUI official website", "strategy": "general", "max_sources": 2}),
}


async def run_probe(tool_id: str, method_name: str, requested: dict[str, Any]) -> dict[str, Any]:
    try:
        module, _ = await load_tool_module_by_id(tool_id)
        tools = module.Tools() if hasattr(module, "Tools") else module
        method = getattr(tools, method_name)
        signature = inspect.signature(method)
        kwargs = {key: value for key, value in requested.items() if key in signature.parameters}
        result = method(**kwargs)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=120)
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        controlled_error = text.startswith(("Error", "Network Error", "Configuration error"))
        return {
            "status": "DEGRADED" if controlled_error else "PASS",
            "method": method_name,
            "signature": str(signature),
            "result_preview": text[:300],
        }
    except BaseException as exc:
        return {
            "status": "FAIL",
            "method": method_name,
            "signature": str(signature) if "signature" in locals() else "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }


async def main() -> None:
    results = {}
    for tool_id, (method_name, kwargs) in PROBES.items():
        results[tool_id] = await run_probe(tool_id, method_name, kwargs)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
