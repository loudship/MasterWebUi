"""
tests/test_gateway_stream_resilience.py
=======================================
Regression tests for the inference-gateway lock/timeout hardening:

1.  A second generation request fails fast with a GPU-busy SSE error frame and
    a terminating [DONE] sentinel instead of queueing forever (P2-2).
2.  A stream whose upstream goes silent mid-generation is aborted by the
    sock_read idle timeout and RELEASES the generation lock (P2-2).
3.  GET /v1/models never waits on the generation lock and is served from the
    short-TTL cache (P1-8).
4.  POST /v1/embeddings uses its own concurrency budget and succeeds while a
    generation holds the GPU lock (P1-8).
5.  Mid-stream upstream errors terminate with [DONE] so OpenAI-compatible
    clients stop spinning (P2-2).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest
from aiohttp import web

pytestmark = pytest.mark.asyncio

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "inference-gateway"
    / "inference_gateway.py"
)


def _load_gateway():
    """Load the gateway module with asyncpg stubbed (host has no asyncpg)."""
    if "asyncpg" not in sys.modules:
        stub = types.ModuleType("asyncpg")

        async def _create_pool(*_a, **_k):  # pragma: no cover - lifespan only
            raise RuntimeError("not used in tests")

        stub.create_pool = _create_pool
        sys.modules["asyncpg"] = stub
    os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://unused/unused")
    os.environ.setdefault("MODEL_ALLOWLIST", "approved-model")
    spec = importlib.util.spec_from_file_location("inference_gateway_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway = _load_gateway()


class FakeOpsPool:
    def __init__(self):
        self.metrics = []

    async def execute(self, _query, *args):
        self.metrics.append(args)

    async def fetchval(self, _query):
        return True


class UpstreamState:
    def __init__(self):
        self.models_hits = 0
        self.hang_forever = False
        self.fail_chat = False


def _build_upstream(state: UpstreamState) -> web.Application:
    async def models(_request):
        state.models_hits += 1
        return web.json_response(
            {"data": [{"id": "approved-model"}, {"id": "blocked-model"}]}
        )

    async def chat(_request):
        if state.fail_chat:
            return web.json_response({"error": "boom"}, status=500)
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream"}
        )
        await response.prepare(_request)
        await response.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
        if state.hang_forever:
            # Simulate a hung upstream socket: never send another byte.
            await asyncio.sleep(3600)
        await response.write(b"data: [DONE]\n\n")
        return response

    async def embeddings(_request):
        return web.json_response({"data": [{"embedding": [0.1] * 4}]})

    upstream = web.Application()
    upstream.router.add_get("/v1/models", models)
    upstream.router.add_post("/v1/chat/completions", chat)
    upstream.router.add_post("/v1/embeddings", embeddings)
    return upstream


@pytest.fixture
async def harness(monkeypatch):
    """Fake upstream + gateway served by in-process uvicorn over loopback.

    httpx.ASGITransport runs the app coroutine to completion before returning,
    which deadlocks on infinite streams — a real socket server is required to
    exercise streaming behaviour.
    """
    import aiohttp
    import uvicorn
    from httpx import AsyncClient

    state = UpstreamState()
    runner = web.AppRunner(_build_upstream(state))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, shutdown_timeout=0.1)
    await site.start()
    upstream_port = runner.addresses[0][1]

    monkeypatch.setattr(gateway, "UPSTREAM_BASE_URL", f"http://127.0.0.1:{upstream_port}")
    monkeypatch.setattr(gateway, "MODEL_ALLOWLIST", {"approved-model"})
    # Fresh lock + cache per test: module-level state must not leak between tests.
    monkeypatch.setattr(gateway, "_inference_lock", asyncio.Lock())
    monkeypatch.setattr(
        gateway, "_models_cache", {"expires": 0.0, "body": b"", "status": 0}
    )

    gateway.app.state.http = aiohttp.ClientSession()
    gateway.app.state.ops_pool = FakeOpsPool()

    config = uvicorn.Config(
        gateway.app, host="127.0.0.1", port=0, log_level="error", lifespan="off"
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    gateway_port = server.servers[0].sockets[0].getsockname()[1]

    client = AsyncClient(base_url=f"http://127.0.0.1:{gateway_port}", timeout=10.0)
    try:
        yield types.SimpleNamespace(client=client, state=state, gateway=gateway)
    finally:
        await client.aclose()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
        await gateway.app.state.http.close()
        await runner.cleanup()


async def _start_stream(client):
    request = client.build_request(
        "POST",
        "/v1/chat/completions",
        json={"model": "approved-model", "stream": True, "messages": []},
    )
    return await client.send(request, stream=True)


async def test_second_stream_fails_fast_with_busy_and_done(harness, monkeypatch):
    monkeypatch.setattr(gateway, "GENERATION_LOCK_WAIT_S", 0.2)
    harness.state.hang_forever = True

    first = await _start_stream(harness.client)
    iterator = first.aiter_bytes()
    assert b"hi" in await iterator.__anext__()  # first stream owns the GPU

    second = await _start_stream(harness.client)
    body = b""
    async for chunk in second.aiter_bytes():
        body += chunk
    assert b"busy" in body.lower()
    assert b"data: [DONE]" in body

    await first.aclose()
    await second.aclose()


async def test_idle_stream_times_out_and_releases_lock(harness, monkeypatch):
    monkeypatch.setattr(gateway, "STREAM_IDLE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(gateway, "GENERATION_LOCK_WAIT_S", 5.0)
    harness.state.hang_forever = True

    response = await _start_stream(harness.client)
    body = b""
    async for chunk in response.aiter_bytes():
        body += chunk
    await response.aclose()

    # The idle timeout produced an error frame and a [DONE] terminator …
    assert b"error" in body
    assert b"data: [DONE]" in body
    # … and the GPU lock was released for the next caller.
    assert not gateway._inference_lock.locked()

    harness.state.hang_forever = False
    healthy = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "approved-model", "stream": True, "messages": []},
    )
    assert b"hi" in healthy.content
    assert b"data: [DONE]" in healthy.content


async def test_models_is_lock_free_and_cached(harness):
    # Hold the generation lock as if a long stream were running.
    await gateway._inference_lock.acquire()
    try:
        response = await harness.client.get("/v1/models")
        assert response.status_code == 200
        payload = response.json()
        assert [m["id"] for m in payload["data"]] == ["approved-model"]
    finally:
        gateway._inference_lock.release()

    hits_after_first = harness.state.models_hits
    response = await harness.client.get("/v1/models")
    assert response.status_code == 200
    assert harness.state.models_hits == hits_after_first  # served from cache


async def test_embeddings_do_not_queue_behind_generation(harness):
    await gateway._inference_lock.acquire()
    try:
        response = await asyncio.wait_for(
            harness.client.post(
                "/v1/embeddings",
                json={"model": "approved-model", "input": "hello"},
            ),
            timeout=2.0,
        )
    finally:
        gateway._inference_lock.release()
    assert response.status_code == 200
    assert response.json()["data"]


async def test_upstream_error_stream_terminates_with_done(harness):
    harness.state.fail_chat = True
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": "approved-model", "stream": True, "messages": []},
    )
    assert b"error" in response.content
    assert b"data: [DONE]" in response.content
    assert not gateway._inference_lock.locked()


async def test_nonstream_busy_returns_503(harness, monkeypatch):
    monkeypatch.setattr(gateway, "GENERATION_LOCK_WAIT_S", 0.2)
    await gateway._inference_lock.acquire()
    try:
        response = await harness.client.post(
            "/v1/chat/completions",
            json={"model": "approved-model", "messages": []},
        )
    finally:
        gateway._inference_lock.release()
    assert response.status_code == 503
    assert "busy" in response.json()["detail"].lower()
