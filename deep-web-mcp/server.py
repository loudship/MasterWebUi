"""
server.py — Deep Web MCP Server entry point.

Architecture
------------
  FastMCP  ──SSE──►  /sse          (MCP tool discovery + invocation)
  FastAPI  ──POST──► /extract/stream  (Crawl4AI SSE progress stream)
  FastAPI  ──GET───► /extract/status/{id}  (polling fallback)
  FastAPI  ──POST──► /discover, /search, /research, /credentials/store
  FastAPI  ──GET───► /health, /health/validation

Modules
-------
  extraction.py  — Crawl4AI engine, task registry, error handling
  mcp_tools.py   — FastMCP instance and @mcp.tool() definitions
  api.py         — FastAPI routes and SSE generator
  research.py    — Deterministic multi-hop research orchestration
  web_discovery.py — SearXNG search, link validation, layout extraction
"""

from __future__ import annotations

import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# Import app last so all sub-modules initialize first.
from api import app  # noqa: E402 (intentional)

if __name__ == "__main__":
    import uvicorn
    os.makedirs("./data", exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
