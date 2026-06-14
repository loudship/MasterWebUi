"""
Telemetry Gateway — Secure Read-Only Workspace Metrics API
============================================================

A lightweight FastAPI microservice that serves workspace state snapshots to
test automation suites (Puppeteer, pytest) without requiring direct database
credentials or user login overrides.

Security model
--------------
* Authentication: X-Telemetry-Token header validated via hmac.compare_digest
  (constant-time — immune to timing attacks).
* Database access: read-only asyncpg pool connected as the `telemetry_ro`
  PostgreSQL role (SELECT-only; no INSERT/UPDATE/DELETE granted).
* Network scope: binds to 0.0.0.0:9200 inside the container, exposed only
  on 127.0.0.1:19200 on the host — unreachable from the WAN.
* Zero mutations: AST-auditable source — no write SQL statements anywhere.

Endpoints
---------
GET  /health                              — liveness probe (no auth)
POST /api/v1/telemetry/snapshot           — full workspace state JSON
GET  /api/v1/telemetry/models             — active model registrations
GET  /api/v1/telemetry/tools              — active tool registrations
GET  /api/v1/telemetry/sessions           — active session count
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader

logger = logging.getLogger("telemetry_gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POSTGRES_OPS_URL: str = os.environ["POSTGRES_OPS_URL"]
OPEN_WEBUI_URL: str = os.getenv("OPEN_WEBUI_URL", "http://open-webui:8080").rstrip("/")
OPEN_WEBUI_API_KEY: str = os.getenv("OPEN_WEBUI_API_KEY", "")
# Load TELEMETRY_TOKEN from secure Docker secret mount, fallback to environment
_TOKEN_PATH = "/run/secrets/telemetry_token"
if os.path.exists(_TOKEN_PATH):
    try:
        with open(_TOKEN_PATH, "r", encoding="utf-8") as _f:
            TELEMETRY_TOKEN = _f.read().strip()
    except Exception as _exc:
        logger.error("[TELEMETRY] Failed to read token from %s: %s", _TOKEN_PATH, _exc)
        TELEMETRY_TOKEN = os.environ["TELEMETRY_TOKEN"]
else:
    TELEMETRY_TOKEN = os.environ["TELEMETRY_TOKEN"]

_HEADER_SCHEME = APIKeyHeader(name="X-Telemetry-Token", auto_error=False)

# ---------------------------------------------------------------------------
# Token validation (constant-time)
# ---------------------------------------------------------------------------


def _verify_token(token: str | None) -> None:
    """Raise HTTP 401 if token is missing or invalid."""
    if not token:
        raise HTTPException(status_code=401, detail="X-Telemetry-Token header required.")
    valid = hmac.compare_digest(
        token.encode("utf-8"),
        TELEMETRY_TOKEN.encode("utf-8"),
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid telemetry token.")


# ---------------------------------------------------------------------------
# Application lifespan — create read-only PG pool
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The DSN must already carry the telemetry_ro credentials (set in compose).
    # The previous implementation rewrote the user portion and stripped the
    # password in the process, so the pool could never authenticate.
    _require_ro_role(POSTGRES_OPS_URL)
    app.state.db = await asyncpg.create_pool(
        POSTGRES_OPS_URL, min_size=1, max_size=4, command_timeout=10
    )
    logger.info("[TELEMETRY] Read-only pool connected to ops database.")
    try:
        yield
    finally:
        await app.state.db.close()
        logger.info("[TELEMETRY] Pool closed.")


def _dsn_username(url: str) -> str:
    """Extract the username from a postgres DSN without altering it."""
    from urllib.parse import urlsplit

    return urlsplit(url).username or ""


def _require_ro_role(url: str) -> None:
    """Refuse to start with anything but the read-only telemetry role.

    Read-onlyness is enforced by the telemetry_ro role's grants (SELECT only);
    connecting as an operator role would silently widen this service's blast
    radius.
    """
    username = _dsn_username(url)
    if username != "telemetry_ro":
        raise RuntimeError(
            f"POSTGRES_OPS_URL must authenticate as 'telemetry_ro', got {username!r}. "
            "Refusing to start with a non-read-only role."
        )


app = FastAPI(
    title="Telemetry Gateway",
    version="1.0.0",
    description="Read-only workspace metrics for test automation.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_error(_req: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": str(exc.detail)}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error(_req: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "Request validation failed.", "detail": exc.errors()}},
    )


@app.exception_handler(Exception)
async def internal_error(_req: Request, exc: Exception):
    logger.exception("[TELEMETRY] Unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": f"{type(exc).__name__}: {str(exc)[:200]}"}},
    )


# ---------------------------------------------------------------------------
# Liveness probe — no auth required
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "service": "telemetry-gateway", "generated_at": time.time()}


# ---------------------------------------------------------------------------
# Authenticated endpoints
# ---------------------------------------------------------------------------


@app.post("/api/v1/telemetry/snapshot")
async def snapshot(request: Request, token: str | None = Security(_HEADER_SCHEME)):
    """Full workspace state snapshot — models, tools, sessions, DB metrics."""
    _verify_token(token)
    models, tools, sessions, db_metrics = await asyncio.gather(
        _fetch_models(),
        _fetch_tools(),
        _fetch_session_count(request.app.state.db),
        _fetch_db_metrics(request.app.state.db),
        return_exceptions=True,
    )
    return {
        "generated_at": time.time(),
        "models": models if not isinstance(models, Exception) else {"error": str(models)},
        "tools": tools if not isinstance(tools, Exception) else {"error": str(tools)},
        "sessions": sessions if not isinstance(sessions, Exception) else {"error": str(sessions)},
        "db_metrics": db_metrics if not isinstance(db_metrics, Exception) else {"error": str(db_metrics)},
    }


@app.get("/api/v1/telemetry/models")
async def telemetry_models(token: str | None = Security(_HEADER_SCHEME)):
    """Active model registrations from Open WebUI."""
    _verify_token(token)
    return await _fetch_models()


@app.get("/api/v1/telemetry/tools")
async def telemetry_tools(token: str | None = Security(_HEADER_SCHEME)):
    """Active tool registrations from Open WebUI."""
    _verify_token(token)
    return await _fetch_tools()


@app.get("/api/v1/telemetry/sessions")
async def telemetry_sessions(request: Request, token: str | None = Security(_HEADER_SCHEME)):
    """Active session count from PostgreSQL."""
    _verify_token(token)
    return await _fetch_session_count(request.app.state.db)


# ---------------------------------------------------------------------------
# Data fetchers — all read-only
# ---------------------------------------------------------------------------


async def _fetch_models() -> dict[str, Any]:
    """Retrieve model list from Open WebUI API (read-only GET)."""
    if not OPEN_WEBUI_API_KEY:
        return {"configured": False, "models": []}
    async with httpx.AsyncClient(
        base_url=OPEN_WEBUI_URL,
        headers={"Authorization": f"Bearer {OPEN_WEBUI_API_KEY}"},
        timeout=15,
    ) as client:
        resp = await client.get("/api/v1/models/")
        resp.raise_for_status()
        data = resp.json()
    models = data if isinstance(data, list) else data.get("data", [])
    return {
        "configured": True,
        "total": len(models),
        "models": [
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "is_active": m.get("is_active"),
                "base_model_id": m.get("base_model_id"),
            }
            for m in models
        ],
    }


async def _fetch_tools() -> dict[str, Any]:
    """Retrieve tool registrations from Open WebUI API (read-only GET)."""
    if not OPEN_WEBUI_API_KEY:
        return {"configured": False, "tools": []}
    async with httpx.AsyncClient(
        base_url=OPEN_WEBUI_URL,
        headers={"Authorization": f"Bearer {OPEN_WEBUI_API_KEY}"},
        timeout=15,
    ) as client:
        resp = await client.get("/api/v1/tools/")
        resp.raise_for_status()
        data = resp.json()
    tools = data if isinstance(data, list) else data.get("data", [])
    return {
        "configured": True,
        "total": len(tools),
        "tools": [
            {"id": t.get("id"), "name": t.get("name"), "is_active": t.get("is_active")}
            for t in tools
        ],
    }


async def _fetch_session_count(pool: asyncpg.Pool) -> dict[str, Any]:
    """Count active sessions via SELECT — no writes."""
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS session_count FROM pg_stat_activity WHERE datname = current_database()"
    )
    return {"active_sessions": int(row["session_count"]) if row else 0}


async def _fetch_db_metrics(pool: asyncpg.Pool) -> dict[str, Any]:
    """Read inference_gateway_metrics from ops DB — SELECT only."""
    rows = await pool.fetch(
        """
        SELECT
            model,
            COUNT(*) AS request_count,
            AVG(duration_ms)::int AS avg_duration_ms,
            SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count
        FROM inference_gateway_metrics
        WHERE created_at > NOW() - INTERVAL '1 hour'
        GROUP BY model
        ORDER BY request_count DESC
        LIMIT 20
        """
    )
    return {
        "window": "1h",
        "model_metrics": [dict(r) for r in rows],
    }
