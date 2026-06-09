"""Canonical, serialized, OpenAI-compatible proxy for the host inference runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiohttp
import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger("inference_gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

UPSTREAM_BASE_URL = os.environ.get(
    "UPSTREAM_BASE_URL", "http://host.docker.internal:1234"
).rstrip("/")
POSTGRES_OPS_URL = os.environ["POSTGRES_OPS_URL"]
MODEL_ALLOWLIST = {
    model.strip()
    for model in os.environ.get("MODEL_ALLOWLIST", "").split(",")
    if model.strip()
}
if not MODEL_ALLOWLIST:
    raise RuntimeError("MODEL_ALLOWLIST must contain at least one operator-approved model ID.")
UPSTREAM_TIMEOUT_S = float(os.environ.get("UPSTREAM_TIMEOUT_S", "180"))

_inference_lock = asyncio.Lock()


async def _record_metric(
    app: FastAPI,
    *,
    trace_id: str,
    endpoint: str,
    model: str | None,
    status_code: int,
    duration_ms: int,
    error: str | None = None,
) -> None:
    try:
        await app.state.ops_pool.execute(
            """
            INSERT INTO inference_gateway_metrics
                (trace_id, endpoint, model, status_code, duration_ms, error)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            trace_id,
            endpoint,
            model,
            status_code,
            duration_ms,
            error,
        )
    except Exception:
        logger.exception("[METRICS] Failed to persist gateway metric trace_id=%s", trace_id)


def _validate_model(payload: dict) -> str | None:
    model = payload.get("model")
    if MODEL_ALLOWLIST and model not in MODEL_ALLOWLIST:
        raise HTTPException(status_code=403, detail=f"Model {model!r} is not allowlisted.")
    return model


def _filter_model_inventory(payload: dict) -> dict:
    payload["data"] = [
        model for model in payload.get("data", []) if model.get("id") in MODEL_ALLOWLIST
    ]
    return payload


def _forward_headers(response: aiohttp.ClientResponse, trace_id: str) -> dict[str, str]:
    headers = {"X-Trace-Id": trace_id}
    content_type = response.headers.get("Content-Type")
    if content_type:
        headers["Content-Type"] = content_type
    return headers


async def _proxy_json(request: Request, upstream_path: str) -> Response:
    payload = await request.json()
    model = _validate_model(payload)
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    started = time.monotonic()
    status_code = 502
    error: str | None = None

    try:
        async with _inference_lock:
            async with request.app.state.http.post(
                f"{UPSTREAM_BASE_URL}{upstream_path}",
                json=payload,
                headers={"X-Trace-Id": trace_id},
                timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_S),
            ) as upstream:
                status_code = upstream.status
                raw = await upstream.read()
                if upstream.status >= 400:
                    error = raw.decode("utf-8", errors="replace")[:1000]
                return Response(
                    content=raw,
                    status_code=upstream.status,
                    headers=_forward_headers(upstream, trace_id),
                )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        error = str(exc)
        raise HTTPException(status_code=502, detail=f"Inference upstream unavailable: {exc}") from exc
    finally:
        await _record_metric(
            request.app,
            trace_id=trace_id,
            endpoint=upstream_path,
            model=model,
            status_code=status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )


async def _proxy_stream(request: Request, upstream_path: str, payload: dict) -> StreamingResponse:
    model = _validate_model(payload)
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())

    async def stream() -> AsyncIterator[bytes]:
        started = time.monotonic()
        status_code = 502
        error: str | None = None
        try:
            async with _inference_lock:
                async with request.app.state.http.post(
                    f"{UPSTREAM_BASE_URL}{upstream_path}",
                    json=payload,
                    headers={"X-Trace-Id": trace_id},
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                ) as upstream:
                    status_code = upstream.status
                    if upstream.status >= 400:
                        error = (await upstream.text())[:1000]
                        yield f"data: {json.dumps({'error': error})}\n\n".encode()
                        return
                    async for chunk in upstream.content.iter_any():
                        yield chunk
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            error = str(exc)
            yield f"data: {json.dumps({'error': error})}\n\n".encode()
        finally:
            await _record_metric(
                request.app,
                trace_id=trace_id,
                endpoint=upstream_path,
                model=model,
                status_code=status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Trace-Id": trace_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _proxy_post(request: Request, upstream_path: str) -> Response:
    payload = await request.json()
    if payload.get("stream") is True:
        return await _proxy_stream(request, upstream_path, payload)
    return await _proxy_json(request, upstream_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = aiohttp.ClientSession(trust_env=False)
    app.state.ops_pool = await asyncpg.create_pool(POSTGRES_OPS_URL, min_size=1, max_size=4)
    await app.state.ops_pool.execute(
        """
        CREATE TABLE IF NOT EXISTS inference_gateway_metrics (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            trace_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            model TEXT,
            status_code INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL,
            error TEXT
        )
        """
    )
    yield
    await app.state.http.close()
    await app.state.ops_pool.close()


app = FastAPI(title="Canonical Inference Gateway", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    details = {"postgres": False, "upstream": False}
    try:
        details["postgres"] = await request.app.state.ops_pool.fetchval("SELECT TRUE")
        async with request.app.state.http.get(
            f"{UPSTREAM_BASE_URL}/v1/models",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as response:
            inventory = await response.json(content_type=None)
            details["upstream"] = (
                response.status == 200
                and bool(_filter_model_inventory(inventory).get("data"))
            )
    except Exception as exc:
        details["error"] = str(exc)
    healthy = bool(details["postgres"] and details["upstream"])
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "details": details},
    )


@app.get("/v1/models")
async def models(request: Request) -> Response:
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    started = time.monotonic()
    status_code = 502
    error: str | None = None
    try:
        async with _inference_lock:
            async with request.app.state.http.get(
                f"{UPSTREAM_BASE_URL}/v1/models",
                headers={"X-Trace-Id": trace_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as upstream:
                status_code = upstream.status
                raw = await upstream.read()
                if upstream.status >= 400:
                    error = raw.decode("utf-8", errors="replace")[:1000]
                elif upstream.status == 200:
                    try:
                        inventory = _filter_model_inventory(json.loads(raw))
                        if not inventory["data"]:
                            status_code = 503
                            error = "No operator-approved model is currently available upstream."
                            raw = json.dumps({"detail": error}).encode()
                        else:
                            raw = json.dumps(inventory).encode()
                    except (json.JSONDecodeError, TypeError, KeyError) as exc:
                        status_code = 502
                        error = f"Invalid upstream model inventory: {exc}"
                        raw = json.dumps({"detail": error}).encode()
                return Response(
                    content=raw,
                    status_code=status_code,
                    headers=_forward_headers(upstream, trace_id),
                )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        error = str(exc)
        raise HTTPException(status_code=502, detail=f"Inference upstream unavailable: {exc}") from exc
    finally:
        await _record_metric(
            request.app,
            trace_id=trace_id,
            endpoint="/v1/models",
            model=None,
            status_code=status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await _proxy_post(request, "/v1/chat/completions")


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    return await _proxy_post(request, "/v1/responses")


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Response:
    return await _proxy_json(request, "/v1/embeddings")
