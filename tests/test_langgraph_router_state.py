"""
tests/test_langgraph_router_state.py
====================================
Regression tests for the pipeline router state fixes (audit P2-3, P2-7):

1.  ThreadRegistry evicts by TTL and by size — no unbounded growth.
2.  conversation_key scopes threads per chat, not per user.
3.  Two concurrent chats from one user resolve to different graph threads.
4.  iter_sse_events aborts with SSEBufferOverflow on a runaway frame.
5.  The global per-process request lock is gone (GPU serialization belongs to
    the inference gateway, not the pipeline relay).
6.  The stream request carries a sock_read idle timeout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "pipelines" / "langgraph_router.py"

spec = importlib.util.spec_from_file_location("langgraph_router_state_test", MODULE_PATH)
router = importlib.util.module_from_spec(spec)
sys.modules["langgraph_router_state_test"] = router
spec.loader.exec_module(router)


def test_thread_registry_evicts_by_ttl(monkeypatch):
    registry = router.ThreadRegistry(max_entries=10, ttl_s=100.0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(router.time, "monotonic", lambda: clock["now"])

    registry.set("chat:a", "thread-1")
    assert registry.get("chat:a") == "thread-1"

    clock["now"] += 101.0
    assert registry.get("chat:a") is None
    assert len(registry) == 0


def test_thread_registry_evicts_by_size():
    registry = router.ThreadRegistry(max_entries=3, ttl_s=3600.0)
    for i in range(5):
        registry.set(f"chat:{i}", f"thread-{i}")
    assert len(registry) == 3
    assert registry.get("chat:0") is None  # oldest evicted
    assert registry.get("chat:4") == "thread-4"


def test_conversation_key_prefers_chat_id():
    body = {"metadata": {"chat_id": "c-123"}}
    assert router.conversation_key(body, "user-1") == "chat:c-123"
    assert router.conversation_key({"chat_id": "c-456"}, "user-1") == "chat:c-456"
    assert router.conversation_key({}, "user-1") == "user:user-1"


def test_concurrent_chats_from_one_user_get_distinct_threads():
    registry = router.ThreadRegistry()
    key_a = router.conversation_key({"metadata": {"chat_id": "a"}}, "u")
    key_b = router.conversation_key({"metadata": {"chat_id": "b"}}, "u")
    registry.set(key_a, "thread-a")
    registry.set(key_b, "thread-b")
    assert registry.get(key_a) == "thread-a"
    assert registry.get(key_b) == "thread-b"


async def test_sse_buffer_overflow_aborts(monkeypatch):
    monkeypatch.setattr(router, "MAX_SSE_BUFFER_CHARS", 1024)

    class EndlessContent:
        def __init__(self):
            self.sent = 0

        def iter_any(self):
            async def generate():
                # A frame that never terminates with \n\n.
                while True:
                    yield b"data: " + b"x" * 512

            return generate()

    with pytest.raises(router.SSEBufferOverflow):
        async for _event in router.iter_sse_events(EndlessContent()):
            pass


def test_global_request_lock_is_gone():
    pipeline = router.Pipeline()
    assert not hasattr(pipeline, "_request_lock")
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "_request_lock" not in source


def test_stream_timeout_includes_sock_read():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "sock_read=self.valves.STREAM_IDLE_TIMEOUT_S" in source
    pipeline = router.Pipeline()
    assert pipeline.valves.STREAM_IDLE_TIMEOUT_S > 0
