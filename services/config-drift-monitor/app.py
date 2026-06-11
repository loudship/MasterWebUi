from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config_drift.baseline import BaselineLoader
from config_drift.schemas import DiffListResponse, HealthResponse, OverviewResponse, RefreshResponse, SnapshotResponse
from config_drift.telemetry import DriftEngine, ReadOnlyOpenWebUIClient


OPEN_WEBUI_URL = os.getenv("OPEN_WEBUI_URL", "http://open-webui:8080")
OPEN_WEBUI_API_KEY = os.getenv("OPEN_WEBUI_API_KEY", "")
BASELINE_PATH = os.getenv("CONFIG_DRIFT_BASELINE_PATH", "/app/config/config-drift-baseline.yaml")
UI_PATH = Path(__file__).with_name("ui")


def create_engine() -> DriftEngine:
    return DriftEngine(
        ReadOnlyOpenWebUIClient(OPEN_WEBUI_URL, OPEN_WEBUI_API_KEY, float(os.getenv("CONFIG_DRIFT_TIMEOUT_S", "15"))),
        BaselineLoader(BASELINE_PATH),
        int(os.getenv("CONFIG_DRIFT_ADMIN_INTERVAL_S", "30")),
        int(os.getenv("CONFIG_DRIFT_WORKSPACE_INTERVAL_S", "30")),
        int(os.getenv("CONFIG_DRIFT_USER_INTERVAL_S", "60")),
        int(os.getenv("CONFIG_DRIFT_CHAT_INTERVAL_S", "60")),
        int(os.getenv("CONFIG_DRIFT_RECENT_CHAT_HOURS", "24")),
        int(os.getenv("CONFIG_DRIFT_CHATS_PER_USER", "20")),
        int(os.getenv("CONFIG_DRIFT_MAX_CHATS", "100")),
    )


engine: DriftEngine | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global engine
    engine = create_engine()
    await engine.start()
    try:
        yield
    finally:
        await engine.stop()


app = FastAPI(title="Configuration Drift Monitor", version="1.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=UI_PATH), name="config-drift-assets")


def current_engine() -> DriftEngine:
    if engine is None:
        raise HTTPException(status_code=503, detail="Telemetry engine is not started.")
    return engine


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": "http_error", "message": str(exc.detail), "detail": exc.detail}})


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "Request validation failed.", "detail": exc.errors()}})


@app.exception_handler(Exception)
async def internal_error(_request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Configuration drift telemetry failed.", "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}})


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(UI_PATH / "index.html", media_type="text/html")


@app.get("/api/v1/health", response_model=HealthResponse)
async def health():
    active = current_engine()
    overview = active.overview()
    return {"status": "ok" if overview["baseline"]["valid"] else "degraded", "baseline_valid": overview["baseline"]["valid"], "open_webui_configured": bool(OPEN_WEBUI_API_KEY), "generated_at": active.generated_at}


@app.get("/api/v1/overview", response_model=OverviewResponse)
async def overview():
    return current_engine().overview()


@app.get("/api/v1/snapshot", response_model=SnapshotResponse)
async def snapshot():
    return current_engine().snapshot()


@app.get("/api/v1/diffs", response_model=DiffListResponse)
async def diffs(
    plane: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    entity: str | None = None,
):
    records = current_engine().current_diffs()
    if plane:
        records = [item for item in records if plane in {item["parent_plane"], item["child_plane"]}]
    if status:
        records = [item for item in records if item["status"] == status]
    if severity:
        records = [item for item in records if item["severity"] == severity]
    if entity:
        records = [item for item in records if entity.lower() in item["entity_label"].lower()]
    return {"count": len(records), "diffs": records}


@app.get("/api/v1/baseline", response_model=dict[str, Any])
async def baseline():
    try:
        return current_engine().baseline_public()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Baseline invalid: {exc}") from exc


@app.post("/api/v1/refresh", response_model=RefreshResponse)
async def refresh():
    active = current_engine()
    try:
        await active.refresh_all(manual=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"status": active.overview()["status"], "generated_at": active.generated_at or time.time(), "event_version": active.event_version}


@app.get("/api/v1/events")
async def events(request: Request):
    async def stream():
        seen = -1
        while not await request.is_disconnected():
            active = current_engine()
            event = "update" if seen != active.event_version else "heartbeat"
            seen = active.event_version
            yield f"event: {event}\ndata: {json.dumps({'event_version': seen, 'generated_at': active.generated_at})}\n\n"
            await asyncio.sleep(15)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/export")
async def export(format: str = Query("markdown", pattern="^(markdown|json)$")):
    snapshot = current_engine().snapshot()
    if format == "json":
        return JSONResponse(snapshot, headers={"Content-Disposition": 'attachment; filename="configuration-drift-snapshot.json"'})
    return PlainTextResponse(markdown_export(snapshot), media_type="text/markdown", headers={"Content-Disposition": 'attachment; filename="configuration-drift-snapshot.md"'})


def markdown_export(snapshot: dict[str, Any]) -> str:
    overview = snapshot["overview"]
    lines = [
        "# Configuration Drift Monitor Snapshot",
        "",
        f"- Status: **{overview['status'].upper()}**",
        f"- Generated: `{overview['generated_at']}`",
        f"- Baseline rules: `{overview['baseline']['rule_count']}`",
        "",
        "## Plane Status",
        "",
    ]
    for plane in overview["planes"]:
        lines.append(f"- **{plane['name']}**: {plane['status']} · {plane['item_count']} items · {plane['latency_ms']} ms")
    lines.extend(["", "## Differential Matrix", ""])
    for item in snapshot["diffs"]:
        lines.extend(
            [
                f"### {item['status'].upper()} · {item['label']}",
                "",
                f"- Path: `{item['logical_path']}`",
                f"- Planes: `{item['parent_plane']} → {item['child_plane']}`",
                f"- Entity: {item['entity_label']}",
                f"- Severity: `{item['severity']}`",
                f"- Expected: `{json.dumps(item['expected'], ensure_ascii=True, default=str)}`",
                f"- Observed: `{json.dumps(item['observed'], ensure_ascii=True, default=str)}`",
                f"- Recommendation: {item['recommendation'] or 'None'}",
                "",
            ]
        )
    return "\n".join(lines)
