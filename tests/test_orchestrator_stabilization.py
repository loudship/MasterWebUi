"""
tests/test_orchestrator_stabilization.py
========================================
Regression tests for the orchestrator/broker stabilization fixes
(audit P2-8 .. P2-13):

1.  HITL broker connect() verifies Redis with a PING — lazy from_url() alone
    reported connected with Redis down.
2.  HITL broker does not set a global socket_timeout (it aborted BLPOP waits
    after 5 s, auto-denying every authorization).
3.  Orchestrator source: model resolution is cached (no /v1/models round-trip
    per LLM call).
4.  Orchestrator source: zero-vector query/upsert is gone; embeddings flow
    through the gateway with a non-degenerate fallback.
5.  Orchestrator source: the 'contradiction' keyword tripwire is gone.
6.  Orchestrator source: /stream registers its graph task in _active_tasks so
    /interrupt can cancel streamed runs.
7.  Functional: _resolve_model caches across calls (single upstream hit).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ORCH_PATH = ROOT / "backend" / "langgraph_orchestrator.py"
BROKER_PATH = ROOT / "backend" / "hitl_broker.py"


def _orch_src() -> str:
    return ORCH_PATH.read_text(encoding="utf-8")


def _broker_src() -> str:
    return BROKER_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# HITL broker (functional, with redis stubbed)
# ---------------------------------------------------------------------------


def _load_broker(ping_side_effect=None):
    redis_pkg = types.ModuleType("redis")
    redis_asyncio = types.ModuleType("redis.asyncio")

    class FakeRedis:
        def __init__(self):
            self.ping = AsyncMock(side_effect=ping_side_effect)
            self.aclose = AsyncMock()
            self.blpop = AsyncMock(return_value=None)
            self.lpush = AsyncMock(return_value=1)

    fake_client = FakeRedis()
    redis_asyncio.from_url = lambda *_a, **_k: fake_client
    redis_asyncio.Redis = FakeRedis
    redis_asyncio.RedisError = type("RedisError", (Exception,), {})
    redis_pkg.asyncio = redis_asyncio

    saved = {name: sys.modules.get(name) for name in ("redis", "redis.asyncio")}
    sys.modules["redis"] = redis_pkg
    sys.modules["redis.asyncio"] = redis_asyncio
    try:
        spec = importlib.util.spec_from_file_location("hitl_broker_stab_test", BROKER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolves cls.__module__
        spec.loader.exec_module(module)
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    return module, fake_client


async def test_broker_connect_verifies_with_ping():
    module, fake_client = _load_broker()
    broker = module.HITLBroker(redis_url="redis://unit-test:6379/0")
    await broker.connect()
    fake_client.ping.assert_awaited_once()
    assert broker.is_connected is True


async def test_broker_connect_fails_closed_when_redis_down():
    module, fake_client = _load_broker(ping_side_effect=ConnectionError("refused"))
    broker = module.HITLBroker(redis_url="redis://unit-test:6379/0")
    await broker.connect()
    assert broker.is_connected is False
    # Fail-closed: authorization is denied, not granted, without Redis.
    result, reason = await broker.request_authorization("dangerous_tool", {})
    assert result == module.AuthResult.DENIED
    assert "redis" in reason.lower()


def test_broker_has_no_global_socket_timeout():
    src = _broker_src()
    assert "socket_timeout=" not in src.replace("socket_connect_timeout=", ""), (
        "A global socket_timeout aborts BLPOP reads early and auto-denies "
        "every HITL authorization before the approval window elapses."
    )


# ---------------------------------------------------------------------------
# Orchestrator source contracts
# ---------------------------------------------------------------------------


def test_orchestrator_caches_resolved_model():
    src = _orch_src()
    assert "_model_cache" in src
    assert "MODEL_CACHE_TTL_S" in src


def test_orchestrator_zero_vector_retrieval_is_gone():
    src = _orch_src()
    assert "[0.0] * 768" not in src, (
        "Cosine distance against a zero vector is undefined — semantic "
        "retrieval must embed via the gateway (audit P2-10)."
    )
    assert "/v1/embeddings" in src
    assert "_embed_text" in src


def test_orchestrator_contradiction_tripwire_removed():
    src = _orch_src()
    assert 'if "contradiction" in state.get("input", "").lower()' not in src, (
        "Any prompt containing the word 'contradiction' burned three LLM "
        "loops and fail-safed (audit P2-13)."
    )


def test_orchestrator_stream_runs_are_interruptible():
    src = _orch_src()
    # /stream must register its graph task for /interrupt cancellation.
    stream_section = src.split('@app.post("/stream")', 1)[1].split('@app.post("/interrupt")', 1)[0]
    assert "_active_tasks[thread_id] = task" in stream_section
    assert "asyncio.create_task(run_graph())" in stream_section


# ---------------------------------------------------------------------------
# Functional: model cache behaviour (gateway helpers extracted via exec of the
# helper functions alone would drag in langgraph imports — emulate instead).
# ---------------------------------------------------------------------------


class _FakeModelsResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self._payload


class _CountingSession:
    def __init__(self):
        self.hits = 0

    def get(self, *_a, **_k):
        self.hits += 1
        return _FakeModelsResponse({"data": [{"id": "approved-model"}]})


@pytest.fixture
def orchestrator_module():
    """Load the orchestrator with its heavy dependencies stubbed out."""
    pytest.importorskip("aiohttp")

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        return mod

    class _Stub:
        def __init__(self, *a, **k):
            pass

        def __call__(self, *a, **k):
            return self

        def __getattr__(self, _name):
            return _Stub()

    stubs = {
        "asyncpg": module("asyncpg", create_pool=AsyncMock(), Pool=object),
        "qdrant_client": module(
            "qdrant_client", AsyncQdrantClient=lambda *a, **k: _Stub(), models=_Stub()
        ),
        "langgraph": module("langgraph"),
        "langgraph.graph": module("langgraph.graph", END="__end__", StateGraph=_Stub),
        "langgraph.checkpoint": module("langgraph.checkpoint"),
        "langgraph.checkpoint.postgres": module("langgraph.checkpoint.postgres"),
        "langgraph.checkpoint.postgres.aio": module(
            "langgraph.checkpoint.postgres.aio", AsyncPostgresSaver=_Stub
        ),
        "langgraph.checkpoint.serde": module("langgraph.checkpoint.serde"),
        "langgraph.checkpoint.serde.jsonplus": module(
            "langgraph.checkpoint.serde.jsonplus", JsonPlusSerializer=_Stub
        ),
        "hitl_broker": module(
            "hitl_broker",
            AuthResult=type("AuthResult", (), {"APPROVED": "APPROVED"}),
            HITLBroker=lambda *a, **k: _Stub(),
            hitl_router=__import__("fastapi").APIRouter(),
            set_broker=lambda *_a: None,
        ),
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    import os
    os.environ.setdefault("POSTGRES_LANGGRAPH_URL", "postgresql://unused/unused")
    os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://unused/unused")
    try:
        spec = importlib.util.spec_from_file_location("orchestrator_stab_test", ORCH_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


async def test_resolve_model_caches_across_calls(orchestrator_module, monkeypatch):
    mod = orchestrator_module
    monkeypatch.setattr(mod, "_model_cache", {"id": None, "expires": 0.0})
    session = _CountingSession()

    first = await mod._resolve_model(session, "trace-1")
    second = await mod._resolve_model(session, "trace-2")
    third = await mod._resolve_model(session, "trace-3")

    assert first == second == third == "approved-model"
    assert session.hits == 1, "model id must be served from the 30s cache"
