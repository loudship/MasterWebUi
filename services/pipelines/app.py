"""Minimal offline Pipelines runtime: manifold + inference-gateway relay.

Open WebUI is configured with OPENAI_API_BASE_URL=http://pipelines:9099/v1, so
this service must expose a real /v1 surface. Two model classes are served:

1. Pipeline models — Pipeline classes discovered in PIPELINES_DIR (the
   ./pipelines volume mount); their streaming generators are re-emitted as
   OpenAI chat.completion.chunk SSE frames.
2. Gateway models — the allowlisted LM Studio models proxied through the
   inference gateway. The workspace presets' base_model_id values are LM
   Studio ids, so they MUST resolve here or every preset is orphaned.

Endpoints
---------
GET  /            — liveness (no auth)
GET  /health      — liveness (no auth)
GET  /v1/models   — pipeline + gateway models (Bearer PIPELINES_API_KEY)
POST /v1/chat/completions — Pipeline.pipe() or pass-through to the gateway
"""

from __future__ import annotations

import asyncio
import hmac
import importlib.util
import inspect
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("pipelines")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PIPELINES_DIR = Path(os.environ.get("PIPELINES_DIR", "/app/pipelines"))
API_KEY = os.environ.get("PIPELINES_API_KEY", "")
INFERENCE_GATEWAY_URL = os.environ.get(
    "INFERENCE_GATEWAY_URL", "http://inference-gateway:4321"
).rstrip("/")
GATEWAY_MODELS_TIMEOUT_S = float(os.environ.get("GATEWAY_MODELS_TIMEOUT_S", "5"))
GATEWAY_COMPLETION_TIMEOUT_S = float(os.environ.get("GATEWAY_COMPLETION_TIMEOUT_S", "200"))
# Must exceed the gateway's own STREAM_IDLE_TIMEOUT_S (120) so the gateway's
# error frame reaches the client before this relay gives up.
GATEWAY_STREAM_IDLE_TIMEOUT_S = float(os.environ.get("GATEWAY_STREAM_IDLE_TIMEOUT_S", "150"))

_bearer = HTTPBearer(auto_error=False)

# pipeline_id -> instance; model_id -> (pipeline_id, sub_model_id)
PIPELINES: dict[str, Any] = {}
MODELS: dict[str, tuple[str, str]] = {}


def _require_key(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="PIPELINES_API_KEY is not configured.")
    token = credentials.credentials if credentials else ""
    if not hmac.compare_digest(token.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _pipeline_models(pipeline_id: str, pipeline: Any) -> list[tuple[str, str, str]]:
    """Return (model_id, sub_id, display_name) entries for one pipeline."""
    listing = getattr(pipeline, "pipelines", None)
    if callable(listing):
        listing = listing()
    if isinstance(listing, list) and listing:
        entries = []
        for sub in listing:
            sub_id = sub.get("id", pipeline_id)
            name = sub.get("name", sub_id)
            entries.append((f"{pipeline_id}.{sub_id}", sub_id, name))
        return entries
    name = getattr(pipeline, "name", pipeline_id)
    return [(pipeline_id, pipeline_id, name)]


async def load_pipelines() -> None:
    """Import every module in PIPELINES_DIR and register its Pipeline class."""
    PIPELINES.clear()
    MODELS.clear()
    if not PIPELINES_DIR.is_dir():
        logger.error("[PIPELINES] Directory %s does not exist.", PIPELINES_DIR)
        return
    for path in sorted(PIPELINES_DIR.glob("*.py")):
        module_id = path.stem
        try:
            spec = importlib.util.spec_from_file_location(f"pipeline_{module_id}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            pipeline_cls = getattr(module, "Pipeline", None)
            if pipeline_cls is None:
                logger.warning("[PIPELINES] %s defines no Pipeline class — skipped.", path.name)
                continue
            pipeline = pipeline_cls()
            pipeline_id = getattr(pipeline, "id", module_id)
            on_startup = getattr(pipeline, "on_startup", None)
            if on_startup is not None:
                result = on_startup()
                if inspect.isawaitable(result):
                    await result
            PIPELINES[pipeline_id] = pipeline
            for model_id, sub_id, name in _pipeline_models(pipeline_id, pipeline):
                MODELS[model_id] = (pipeline_id, sub_id)
                logger.info("[PIPELINES] Registered model %r (%s)", model_id, name)
        except Exception:
            logger.exception("[PIPELINES] Failed to load %s", path.name)


async def shutdown_pipelines() -> None:
    for pipeline in PIPELINES.values():
        on_shutdown = getattr(pipeline, "on_shutdown", None)
        if on_shutdown is None:
            continue
        try:
            result = on_shutdown()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("[PIPELINES] on_shutdown failed for %r", pipeline)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = aiohttp.ClientSession(trust_env=False)
    await load_pipelines()
    if not MODELS:
        logger.error(
            "[PIPELINES] No pipeline models registered from %s — "
            "Open WebUI will see an empty model list.",
            PIPELINES_DIR,
        )
    yield
    await shutdown_pipelines()
    await app.state.http.close()


app = FastAPI(title="Hardened Pipelines Runtime", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "healthy", "pipelines": sorted(PIPELINES)}


@app.get("/health")
async def health():
    return {"status": "healthy", "models": len(MODELS)}


async def _gateway_models(request: Request) -> list[dict]:
    """Fetch the allowlisted LM Studio inventory via the inference gateway.

    The workspace presets' base_model_id values are LM Studio ids — if this
    list is empty every preset is orphaned, so failures are logged loudly but
    never take the pipeline models down with them.
    """
    try:
        async with request.app.state.http.get(
            f"{INFERENCE_GATEWAY_URL}/v1/models",
            timeout=aiohttp.ClientTimeout(total=GATEWAY_MODELS_TIMEOUT_S),
        ) as upstream:
            if upstream.status != 200:
                logger.warning("[PIPELINES] Gateway model list HTTP %s", upstream.status)
                return []
            payload = await upstream.json(content_type=None)
            return [model for model in payload.get("data", []) if model.get("id")]
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        logger.warning("[PIPELINES] Gateway model list unavailable: %s", exc)
        return []


@app.get("/v1/models")
async def models(request: Request, _: None = Depends(_require_key)) -> dict:
    data = []
    for model_id, (pipeline_id, _sub_id) in MODELS.items():
        pipeline = PIPELINES[pipeline_id]
        data.append(
            {
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "pipelines",
                "name": getattr(pipeline, "name", model_id),
                "pipeline": {"type": getattr(pipeline, "type", "pipe")},
            }
        )
    local_ids = {entry["id"] for entry in data}
    for model in await _gateway_models(request):
        if model["id"] not in local_ids:
            data.append(model)
    return {"object": "list", "data": data}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def _last_user_message(messages: list[dict]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _message_text(message.get("content", ""))
    return ""


def _chunk_frame(completion_id: str, model_id: str, content: str | None, finish: str | None) -> bytes:
    delta = {"content": content} if content is not None else {}
    frame = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(frame)}\n\n".encode()


async def _iter_result(result: Any) -> AsyncIterator[str]:
    if isinstance(result, str):
        yield result
        return
    if inspect.isasyncgen(result):
        async for item in result:
            yield str(item)
        return
    if inspect.isgenerator(result):
        for item in result:
            yield str(item)
        return
    yield str(result)


async def _relay_to_gateway(request: Request, body: dict) -> Response:
    """Pass a completion for a non-pipeline model through to the gateway.

    The gateway owns the allowlist, GPU serialization, and stream idle
    timeouts — this relay adds no policy of its own.
    """
    if body.get("stream") is True:

        async def stream() -> "AsyncIterator[bytes]":
            try:
                async with request.app.state.http.post(
                    f"{INFERENCE_GATEWAY_URL}/v1/chat/completions",
                    json=body,
                    timeout=aiohttp.ClientTimeout(
                        total=None,
                        sock_connect=10,
                        sock_read=GATEWAY_STREAM_IDLE_TIMEOUT_S,
                    ),
                ) as upstream:
                    if upstream.status >= 400:
                        detail = (await upstream.text())[:500]
                        yield f"data: {json.dumps({'error': detail})}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return
                    async for chunk in upstream.content.iter_any():
                        yield chunk
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n".encode()
                yield b"data: [DONE]\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        async with request.app.state.http.post(
            f"{INFERENCE_GATEWAY_URL}/v1/chat/completions",
            json=body,
            timeout=aiohttp.ClientTimeout(total=GATEWAY_COMPLETION_TIMEOUT_S),
        ) as upstream:
            raw = await upstream.read()
            return Response(
                content=raw,
                status_code=upstream.status,
                media_type=upstream.headers.get("Content-Type", "application/json"),
            )
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"Inference gateway unavailable: {exc}") from exc


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _: None = Depends(_require_key)):
    body = await request.json()
    model_id = body.get("model", "")
    if model_id not in MODELS:
        # Not a pipeline model: presets resolve LM Studio ids through the
        # gateway relay (404ing here orphans every workspace preset).
        return await _relay_to_gateway(request, body)
    pipeline_id, sub_id = MODELS[model_id]
    pipeline = PIPELINES[pipeline_id]
    messages = body.get("messages", [])
    user_message = _last_user_message(messages)

    result = pipeline.pipe(
        user_message=user_message,
        model_id=sub_id,
        messages=messages,
        body=body,
    )
    if inspect.isawaitable(result):
        result = await result

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    if body.get("stream") is True:

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for part in _iter_result(result):
                    if part:
                        yield _chunk_frame(completion_id, model_id, part, None)
            except Exception as exc:
                logger.exception("[PIPELINES] Stream relay failed for %r", model_id)
                yield _chunk_frame(completion_id, model_id, f"Pipeline Error: {exc}", None)
            yield _chunk_frame(completion_id, model_id, None, "stop")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    parts = [part async for part in _iter_result(result)]
    content = "".join(parts)
    return JSONResponse(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    )
