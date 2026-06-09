"""Run inside langgraph-orchestrator:hardened for graph-loop contract checks."""

from __future__ import annotations

import asyncio
import inspect
import operator
import os
import sys
from typing import get_type_hints

sys.path.insert(0, "/app/orchestrator")
os.environ.setdefault("POSTGRES_LANGGRAPH_URL", "postgresql://unused/unused")
os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://unused/unused")
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

import langgraph_orchestrator as orchestrator


class FakeQdrant:
    async def upsert(self, **_kwargs):
        return None


def state(remaining_loops: int, attempt: int, proposal: str = "candidate"):
    return orchestrator._build_initial_state(
        "narrative",
        thread_id="thread-contract",
        trace_id="trace-contract",
    ) | {
        "initial_pristine_payload": "pristine lore",
        "current_story_arc": "pristine lore",
        "remaining_loops": remaining_loops,
        "volatile_buffer": {"proposal": proposal, "attempt": attempt},
    }


async def main() -> None:
    assert orchestrator.TOTAL_ALLOWED_LOOPS == 3
    annotations = get_type_hints(orchestrator.NarrativeState, include_extras=True)
    assert annotations["message_array"].__metadata__[0] is operator.add
    assert annotations["execution_log"].__metadata__[0] is operator.add

    async def contradiction(*_args, **_kwargs):
        return "CONTRADICTION: contract"

    orchestrator._llm_call = contradiction
    remaining = 3
    for attempt, expected in enumerate((2, 1, 0), start=1):
        result = await orchestrator.Continuity_Verifier(state(remaining, attempt))
        remaining = result["remaining_loops"]
        assert remaining == expected
    assert orchestrator.route_verifier(state(0, 3)) == "fail_safe_termination"

    async def ok(*_args, **_kwargs):
        return "OK"

    orchestrator._llm_call = ok
    orchestrator.qdrant_client = FakeQdrant()
    for attempt, remaining in ((1, 3), (2, 2), (3, 1)):
        result = await orchestrator.Continuity_Verifier(state(remaining, attempt))
        assert result["consensus_reached"] is True
        assert result.get("remaining_loops", remaining) == remaining

    result = await orchestrator.fail_safe_termination(state(0, 3))
    assert result["volatile_buffer"] == {}
    assert result["current_story_arc"] == "pristine lore"
    assert result["retry_count"] == 3
    assert result["termination_reason"]

    assert ".adelete_thread(" in inspect.getsource(orchestrator.interrupt_graph)
    assert ".aget(" in inspect.getsource(orchestrator.debug_state)
    print("orchestrator-container-contract-ok")


if __name__ == "__main__":
    asyncio.run(main())
