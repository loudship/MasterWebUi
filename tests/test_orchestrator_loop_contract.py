import importlib
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("qdrant_client")
pytest.importorskip("asyncpg")
pytest.importorskip("langgraph.checkpoint.postgres")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("POSTGRES_LANGGRAPH_URL", "postgresql://unused/unused")
os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://unused/unused")

orchestrator = importlib.import_module("langgraph_orchestrator")


def _state(remaining_loops):
    return orchestrator._build_initial_state(
        "force contradiction",
        thread_id="thread-test",
        trace_id="trace-test",
    ) | {
        "initial_pristine_payload": "pristine lore",
        "current_story_arc": "pristine lore",
        "remaining_loops": remaining_loops,
        "volatile_buffer": {
            "proposal": "candidate",
            "attempt": orchestrator.TOTAL_ALLOWED_LOOPS - remaining_loops + 1,
        },
    }


@pytest.mark.asyncio
async def test_exactly_three_failed_attempts_route_to_fail_safe(monkeypatch):
    async def contradiction(*args, **kwargs):
        return "CONTRADICTION: test"

    monkeypatch.setattr(orchestrator, "_llm_call", contradiction)
    remaining = orchestrator.TOTAL_ALLOWED_LOOPS
    for expected_remaining in (2, 1, 0):
        result = await orchestrator.Continuity_Verifier(_state(remaining))
        remaining = result["remaining_loops"]
        assert remaining == expected_remaining
        route = orchestrator.route_verifier(_state(remaining) | result)
    assert route == "fail_safe_termination"


@pytest.mark.asyncio
async def test_fail_safe_clears_buffer_and_restores_pristine():
    result = await orchestrator.fail_safe_termination(_state(0))
    assert result["volatile_buffer"] == {}
    assert result["current_story_arc"] == "pristine lore"
    assert result["remaining_loops"] == 0
    assert result["retry_count"] == orchestrator.TOTAL_ALLOWED_LOOPS
    assert result["termination_reason"]
    assert result["execution_log"]
