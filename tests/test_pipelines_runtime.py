"""
tests/test_pipelines_runtime.py
===============================
Contract tests for the offline Pipelines runtime (services/pipelines/app.py).

The previous implementation was a healthcheck stub with no /v1 surface, which
silently killed the entire Open WebUI → pipelines model path (audit P2-1).
These tests pin the contract Open WebUI actually depends on:

1.  /v1/models requires the PIPELINES_API_KEY bearer token.
2.  /v1/models lists the model exposed by pipelines/langgraph_router.py.
3.  /v1/chat/completions relays to Pipeline.pipe() (non-stream JSON shape).
4.  /v1/chat/completions with stream=true emits OpenAI chat.completion.chunk
    frames and terminates with data: [DONE].
5.  Unknown models return 404, not a hang.
6.  The compose healthcheck asserts /v1/models, not bare process liveness.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "services" / "pipelines" / "app.py"

TEST_KEY = "test-pipelines-key"


class GatewayState:
    def __init__(self):
        self.models = [{"id": "qwen-test-model", "object": "model"}]
        self.completion_calls = []


def _build_fake_gateway(state: GatewayState):
    from aiohttp import web

    async def models(_request):
        return web.json_response({"object": "list", "data": state.models})

    async def chat(request):
        body = await request.json()
        state.completion_calls.append(body)
        return web.json_response(
            {
                "id": "chatcmpl-gw",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "gateway says hi"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    gateway = web.Application()
    gateway.router.add_get("/v1/models", models)
    gateway.router.add_post("/v1/chat/completions", chat)
    return gateway


@pytest.fixture()
async def runtime(monkeypatch, tmp_path):
    """Load the runtime against the real ./pipelines directory + fake gateway."""
    import aiohttp
    from aiohttp import web

    gateway_state = GatewayState()
    runner = web.AppRunner(_build_fake_gateway(gateway_state))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    gateway_port = runner.addresses[0][1]

    monkeypatch.setenv("PIPELINES_API_KEY", TEST_KEY)
    monkeypatch.setenv("PIPELINES_DIR", str(ROOT / "pipelines"))
    monkeypatch.setenv("LANGGRAPH_URL", "http://langgraph-orchestrator.invalid:8100")
    monkeypatch.setenv("INFERENCE_GATEWAY_URL", f"http://127.0.0.1:{gateway_port}")

    spec = importlib.util.spec_from_file_location("pipelines_runtime_test", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.load_pipelines()
    module.app.state.http = aiohttp.ClientSession(trust_env=False)

    client = AsyncClient(
        transport=ASGITransport(app=module.app), base_url="http://pipelines"
    )
    try:
        yield types.SimpleNamespace(
            module=module, client=client, gateway=gateway_state
        )
    finally:
        await client.aclose()
        await module.shutdown_pipelines()
        await module.app.state.http.close()
        await runner.cleanup()


def _auth() -> dict:
    return {"Authorization": f"Bearer {TEST_KEY}"}


async def test_models_requires_api_key(runtime):
    assert (await runtime.client.get("/v1/models")).status_code == 401
    bad = await runtime.client.get(
        "/v1/models", headers={"Authorization": "Bearer wrong-key"}
    )
    assert bad.status_code == 401


async def test_models_lists_langgraph_manifold(runtime):
    response = await runtime.client.get("/v1/models", headers=_auth())
    assert response.status_code == 200
    data = response.json()["data"]
    assert data, "runtime registered no models — Open WebUI would see nothing"
    ids = [m["id"] for m in data]
    assert any("hardened_langgraph" in model_id for model_id in ids)


async def test_chat_completion_relays_pipe_result(runtime):
    response = await runtime.client.get("/v1/models", headers=_auth())
    model_id = response.json()["data"][0]["id"]

    pipeline_id, _sub = runtime.module.MODELS[model_id]
    pipeline = runtime.module.PIPELINES[pipeline_id]

    async def fake_pipe(user_message, model_id, messages, body):
        return f"echo:{user_message}"

    pipeline.pipe = fake_pipe

    response = await runtime.client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "echo:ping"


async def test_chat_completion_streams_openai_chunks(runtime):
    response = await runtime.client.get("/v1/models", headers=_auth())
    model_id = response.json()["data"][0]["id"]
    pipeline_id, _sub = runtime.module.MODELS[model_id]
    pipeline = runtime.module.PIPELINES[pipeline_id]

    async def fake_pipe(user_message, model_id, messages, body):
        async def generate():
            yield "Hello "
            yield "world"

        return generate()

    pipeline.pipe = fake_pipe

    response = await runtime.client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={
            "model": model_id,
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    body = response.text
    frames = [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    contents = [
        frame["choices"][0]["delta"].get("content", "")
        for frame in frames
        if frame["object"] == "chat.completion.chunk"
    ]
    assert "".join(contents) == "Hello world"
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    assert body.rstrip().endswith("data: [DONE]")


async def test_models_merges_gateway_inventory(runtime):
    """Workspace presets reference LM Studio ids (base_model_id) — the model
    list must include the gateway's allowlisted inventory or every preset is
    orphaned."""
    response = await runtime.client.get("/v1/models", headers=_auth())
    ids = [m["id"] for m in response.json()["data"]]
    assert "qwen-test-model" in ids
    assert any("hardened_langgraph" in model_id for model_id in ids)


async def test_non_pipeline_model_relays_to_gateway(runtime):
    response = await runtime.client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={
            "model": "qwen-test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "gateway says hi"
    assert runtime.gateway.completion_calls[0]["model"] == "qwen-test-model"


async def test_unknown_model_with_gateway_down_fails_fast(runtime, monkeypatch):
    monkeypatch.setattr(
        runtime.module, "INFERENCE_GATEWAY_URL", "http://127.0.0.1:9"  # closed port
    )
    response = await runtime.client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={"model": "ghost.model", "messages": []},
    )
    assert response.status_code == 502


def test_compose_healthcheck_asserts_models_contract():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    test_cmd = " ".join(compose["services"]["pipelines"]["healthcheck"]["test"])
    assert "/v1/models" in test_cmd, "healthcheck must assert the /v1 contract"
    assert "PIPELINES_API_KEY" in test_cmd
