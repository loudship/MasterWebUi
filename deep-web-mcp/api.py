"""
api.py — FastAPI HTTP surface: REST routes, SSE streaming, credential storage.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from database import save_credentials
from extraction import (
    crawl4ai_extract,
    get_task,
    new_task,
)
from mcp_tools import ResearchInput, WebDiscoveryInput, mcp
from research import research_web as run_research_web
from web_discovery import (
    discover_web_layouts as run_web_discovery,
    validate_supporting_endpoints as validate_web_dependencies,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Deep Web MCP",
    description="Search and extraction bridge for the local web-tool stack.",
    version="3.2.0",
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status":                 "ok",
        "service":                "deep-web-mcp",
        "tools": [
            "fetch_deep_web_data",
            "search_deep_web_database",
            "discover_web_layouts",
            "research_web",
        ],
    }


@app.get("/health/validation")
async def health_validation():
    try:
        services = await validate_web_dependencies()
        return {"status": "ok", "services": services}
    except Exception as exc:
        logger.exception("[HEALTH] validation probe failed.")
        return JSONResponse(
            status_code=500,
            content={
                "status":     "error",
                "error_code": "HEALTH_VALIDATION_FAILED",
                "reason":     f"{type(exc).__name__}: {exc}",
            },
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    target_database:  str  = Field("bing", description="SearXNG engine identifier.")
    search_query:     str  = Field(..., min_length=1, max_length=500)
    session_required: bool = False


@app.post("/search")
async def search(req: SearchRequest):
    from mcp_tools import search_deep_web_database
    return json.loads(await search_deep_web_database(
        target_database=req.target_database,
        search_query=req.search_query,
        session_required=req.session_required,
    ))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@app.post("/discover")
async def discover(req: WebDiscoveryInput):
    try:
        result = await run_web_discovery(
            req.query,
            domain_filters=[f.model_dump() for f in req.domain_filters],
            max_tokens=req.max_tokens,
            max_chars=req.max_chars,
            max_results=req.max_results,
        )
        if isinstance(result, dict) and result.get("status") == "error":
            return JSONResponse(status_code=422, content=result)
        return result
    except Exception as exc:
        logger.exception("[DISCOVERY] Unhandled failure.")
        return JSONResponse(
            status_code=500,
            content={
                "status":     "error",
                "error_code": "DISCOVERY_FAILED",
                "reason":     f"{type(exc).__name__}: {exc}",
            },
        )


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------
@app.post("/research")
async def research(req: ResearchInput):
    try:
        result = await run_research_web(
            query=req.query,
            strategy=req.strategy,
            domain_filters=[item.model_dump() for item in req.domain_filters],
            max_iterations=req.max_iterations,
            max_sources=req.max_sources,
            max_hops=req.max_hops,
            total_budget_s=req.total_budget_s,
        )
        if result.get("status") == "error":
            return JSONResponse(status_code=422, content=result)
        return result
    except Exception as exc:
        logger.exception("[RESEARCH] Unhandled failure.")
        return JSONResponse(
            status_code=500,
            content={
                "status":     "error",
                "error_code": "RESEARCH_FAILED",
                "reason":     f"{type(exc).__name__}: {exc}",
            },
        )


# ---------------------------------------------------------------------------
# Extraction — SSE streaming
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    url:              str
    thread_id:        str           = Field("default")
    session_required: bool          = Field(False)
    js_eval:          Optional[str] = Field(None)


@app.post("/extract/stream")
async def extract_stream(req: ExtractRequest):
    """Launch a Crawl4AI extraction and stream SSE progress frames."""
    task_id = new_task(req.url)

    async def _run_extraction() -> dict:
        return await crawl4ai_extract(
            url=req.url,
            thread_id=req.thread_id,
            session_required=req.session_required,
            js_eval=req.js_eval,
            task_id=task_id,
        )

    bg_task = asyncio.create_task(_run_extraction())

    async def _sse_generator():
        last_pct = -1
        try:
            yield {
                "event": "progress",
                "data":  json.dumps({"task_id": task_id, "progress": 0, "status": "running", "url": req.url}),
            }
            while not bg_task.done():
                await asyncio.sleep(0.25)
                entry = get_task(task_id)
                if entry and entry.progress != last_pct:
                    last_pct = entry.progress
                    yield {
                        "event": "progress",
                        "data":  json.dumps({"task_id": task_id, "progress": entry.progress, "status": entry.status}),
                    }
        finally:
            if not bg_task.done():
                bg_task.cancel()

        try:
            result = await bg_task
        except asyncio.CancelledError:
            return
        except Exception as exc:
            yield {
                "event": "error",
                "data":  json.dumps({"task_id": task_id, "error_code": "TASK_EXCEPTION", "reason": str(exc)}),
            }
            return

        if result.get("status") == "success":
            yield {
                "event": "progress",
                "data":  json.dumps({"task_id": task_id, "progress": 100, "status": "done"}),
            }
            yield {
                "event": "result",
                "data":  json.dumps({
                    "task_id":     task_id,
                    "content":     result.get("content", ""),
                    "source":      "live",
                    "truncated":   result.get("truncated", False),
                    "links_found": result.get("links_found", 0),
                }),
            }
        else:
            yield {
                "event": "error",
                "data":  json.dumps({
                    "task_id":    task_id,
                    "error_code": result.get("error_code", "UNKNOWN"),
                    "reason":     result.get("reason", "Unknown extraction failure."),
                }),
            }

    return EventSourceResponse(_sse_generator())


# ---------------------------------------------------------------------------
# Extraction — polling fallback
# ---------------------------------------------------------------------------
@app.get("/extract/status/{task_id}")
async def extract_status(task_id: str):
    task = get_task(task_id)
    if not task:
        return {"error": f"No task found for task_id={task_id!r}"}
    return {
        "task_id":    task.task_id,
        "url":        task.url,
        "progress":   task.progress,
        "status":     task.status,
        "result":     task.result,
        "started_at": task.started_at,
    }


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------
class CredentialStoreRequest(BaseModel):
    thread_id:  str
    auth_array: list


@app.post("/credentials/store")
async def store_credentials(req: CredentialStoreRequest):
    try:
        save_credentials(domain_id=req.thread_id, payload=req.auth_array)
        return {"status": "ok", "thread_id": req.thread_id, "entries": len(req.auth_array)}
    except Exception as exc:
        logger.exception("[CREDENTIALS] Store failed for thread_id=%r.", req.thread_id)
        return {"status": "error", "reason": str(exc)}


# Mount MCP SSE transport last so explicit REST routes keep precedence.
app.mount("/", mcp.sse_app())
