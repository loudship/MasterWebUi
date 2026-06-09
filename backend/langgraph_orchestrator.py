"""Durable LangGraph orchestration service for the hardened air-gapped runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import operator
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Optional, TypedDict

import aiohttp
import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient, models

from hitl_broker import AuthResult, HITLBroker, hitl_router, set_broker

logger = logging.getLogger("langgraph_orchestrator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOTAL_ALLOWED_LOOPS = 3
DEFAULT_CANON = "Default Canon: Time travel is strictly forbidden. Magic requires physical sacrifice."

QDRANT_URI = os.environ.get("QDRANT_URI", "http://qdrant:6333")
QDRANT_NARRATIVE_ALIAS = os.environ.get("QDRANT_NARRATIVE_ALIAS", "narrative_active")
QDRANT_NARRATIVE_BOOTSTRAP_COLLECTION = os.environ.get(
    "QDRANT_NARRATIVE_BOOTSTRAP_COLLECTION", "primary_narrative"
)
INFERENCE_GATEWAY_URL = os.environ.get(
    "INFERENCE_GATEWAY_URL", "http://inference-gateway:4321"
).rstrip("/")
POSTGRES_LANGGRAPH_URL = os.environ["POSTGRES_LANGGRAPH_URL"]
POSTGRES_OPS_URL = os.environ["POSTGRES_OPS_URL"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis-hitl:6379/0")

qdrant_client = AsyncQdrantClient(url=QDRANT_URI, prefer_grpc=False)
broker = HITLBroker(redis_url=REDIS_URL)

_active_tasks: dict[str, asyncio.Task] = {}
_graph = None
_checkpointer: AsyncPostgresSaver | None = None
_checkpointer_context = None
_ops_pool: asyncpg.Pool | None = None


class NarrativeState(TypedDict):
    message_array: Annotated[list[str], operator.add]
    execution_log: Annotated[list[str], operator.add]
    validation_errors: Annotated[list[str], operator.add]
    volatile_buffer: dict
    input: str
    messages: list[dict]
    intent: str
    metadata_filter: dict
    current_story_arc: str
    initial_pristine_payload: str
    final_output: str
    thread_id: str
    trace_id: str
    remaining_loops: int
    retry_count: int
    consensus_reached: bool
    termination_reason: str


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


async def _resolve_model(session: aiohttp.ClientSession, trace_id: str) -> str:
    try:
        async with session.get(
            f"{INFERENCE_GATEWAY_URL}/v1/models",
            headers={"X-Trace-Id": trace_id},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            data = await response.json(content_type=None)
            if response.status == 200 and data.get("data"):
                return data["data"][0]["id"]
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        logger.exception("[GATEWAY] Model discovery failed.")
    return "local-model"


async def _llm_call(
    session: aiohttp.ClientSession,
    *,
    trace_id: str,
    system: str,
    user: str,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> str:
    model = await _resolve_model(session, trace_id)
    try:
        async with session.post(
            f"{INFERENCE_GATEWAY_URL}/v1/chat/completions",
            headers={"X-Trace-Id": trace_id},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            data = await response.json(content_type=None)
            if response.status == 200:
                return data["choices"][0]["message"]["content"]
            return f"[GATEWAY HTTP {response.status}: {str(data)[:300]}]"
    except asyncio.TimeoutError:
        return "[GATEWAY TIMEOUT]"
    except (aiohttp.ClientError, OSError, KeyError, ValueError) as exc:
        return f"[GATEWAY UNREACHABLE: {exc}]"


async def _persist_execution_event(
    *,
    thread_id: str,
    trace_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if _ops_pool is None:
        return
    try:
        await _ops_pool.execute(
            """
            INSERT INTO orchestration_events (thread_id, trace_id, event_type, payload)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            thread_id,
            trace_id,
            event_type,
            json.dumps(payload),
        )
    except Exception:
        logger.exception("[OPS] Failed to persist orchestration event.")


async def _ensure_narrative_alias() -> None:
    aliases = await qdrant_client.get_aliases()
    if any(alias.alias_name == QDRANT_NARRATIVE_ALIAS for alias in aliases.aliases):
        return

    if not await qdrant_client.collection_exists(QDRANT_NARRATIVE_BOOTSTRAP_COLLECTION):
        raise RuntimeError(
            "Qdrant narrative alias is absent and bootstrap collection "
            f"{QDRANT_NARRATIVE_BOOTSTRAP_COLLECTION!r} does not exist. "
            "Run the migration/bootstrap job before starting the orchestrator."
        )

    await qdrant_client.update_collection_aliases(
        change_aliases_operations=[
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=QDRANT_NARRATIVE_BOOTSTRAP_COLLECTION,
                    alias_name=QDRANT_NARRATIVE_ALIAS,
                )
            )
        ]
    )
    logger.info(
        "[QDRANT] Bootstrapped alias %s -> %s",
        QDRANT_NARRATIVE_ALIAS,
        QDRANT_NARRATIVE_BOOTSTRAP_COLLECTION,
    )


async def Semantic_Router_Node(state: NarrativeState) -> dict:
    user_input = state.get("input", "")
    lower = user_input.lower()
    if any(token in lower for token in ("http://", "https://", "website", "crawl", "scrape")):
        intent = "web"
    elif any(token in lower for token in ("contradiction", "timeline", "lore", "story", "narrative", "world")):
        intent = "narrative"
    else:
        intent = "factual"
    metadata_filter = {"content_type": intent}
    return {
        "intent": intent,
        "metadata_filter": metadata_filter,
        "execution_log": [f"[ROUTER] intent={intent}"],
    }


def route_after_semantic(state: NarrativeState) -> str:
    return "Factual_Shortcircuit_Node" if state.get("intent") == "factual" else "Lorekeeper_Node"


async def Factual_Shortcircuit_Node(state: NarrativeState) -> dict:
    async with aiohttp.ClientSession(trust_env=False) as session:
        answer = await _llm_call(
            session,
            trace_id=state["trace_id"],
            system="Answer concisely and factually.",
            user=state.get("input", "")[:20_000],
            temperature=0.2,
            max_tokens=512,
        )
    return {
        "final_output": answer,
        "message_array": [answer],
        "consensus_reached": True,
        "execution_log": ["[FACTUAL] Completed without recursive deliberation."],
    }


async def Lorekeeper_Node(state: NarrativeState) -> dict:
    arc = DEFAULT_CANON
    search_filter = None
    content_type = state.get("metadata_filter", {}).get("content_type")
    if content_type:
        search_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="content_type",
                    match=models.MatchValue(value=content_type),
                )
            ]
        )
    try:
        response = await qdrant_client.query_points(
            collection_name=QDRANT_NARRATIVE_ALIAS,
            query=[0.0] * 768,
            query_filter=search_filter,
            limit=5,
            with_payload=True,
        )
        if response.points:
            arc = response.points[0].payload.get("consensus_proposal", arc)
    except Exception as exc:
        logger.warning("[LOREKEEPER] Alias retrieval fallback: %s", exc)

    return {
        "current_story_arc": arc,
        "initial_pristine_payload": arc,
        "volatile_buffer": {},
        "remaining_loops": TOTAL_ALLOWED_LOOPS,
        "retry_count": 0,
        "consensus_reached": False,
        "termination_reason": "",
        "execution_log": [
            f"[LOREKEEPER] Pristine checkpoint loaded via alias={QDRANT_NARRATIVE_ALIAS} hash={_sha8(arc)}."
        ],
    }


HIGH_RISK_TOOL_MARKERS = frozenset(
    {"DELETE", "DROP TABLE", "FORMAT DISK", "SYSTEM CALL", "OVERRIDE SAFETY", "EXECUTE PAYLOAD", "INJECT"}
)


async def Simulator(state: NarrativeState) -> dict:
    attempt = TOTAL_ALLOWED_LOOPS - state.get("remaining_loops", TOTAL_ALLOWED_LOOPS) + 1
    prior_errors = state.get("validation_errors", [])
    correction = "\n".join(prior_errors[-3:])
    async with aiohttp.ClientSession(trust_env=False) as session:
        proposal = await _llm_call(
            session,
            trace_id=state["trace_id"],
            system=(
                f"STATIC WORLD LAWS:\n{state.get('current_story_arc', '')}\n\n"
                f"You are the World-State Simulator. Attempt {attempt} of {TOTAL_ALLOWED_LOOPS}. "
                "Produce a continuation consistent with every world law."
                f"\nPrior validation errors:\n{correction}"
            ),
            user=state.get("input", ""),
            temperature=max(0.3, 0.7 - state.get("retry_count", 0) * 0.1),
        )

    risk_markers = [marker for marker in HIGH_RISK_TOOL_MARKERS if marker in proposal.upper()]
    if risk_markers:
        call_id = str(uuid.uuid4())
        result, reason = await broker.request_authorization(
            tool_name="narrative_tool",
            tool_args={"markers": risk_markers, "proposal_hash": _sha8(proposal)},
            call_id=call_id,
            timeout=float(os.environ.get("HITL_TIMEOUT_S", "120")),
        )
        if result != AuthResult.APPROVED:
            return {
                "volatile_buffer": {},
                "validation_errors": [f"[HITL BLOCKED] {result}: {reason}"],
                "execution_log": [f"[SIMULATOR] Attempt {attempt} blocked by HITL."],
            }

    proposal_hash = _sha8(proposal)
    return {
        "volatile_buffer": {
            "proposal": proposal,
            "hash": proposal_hash,
            "attempt": attempt,
            "timestamp": time.time(),
        },
        "execution_log": [f"[SIMULATOR] Attempt {attempt} staged hash={proposal_hash}."],
    }


async def Continuity_Verifier(state: NarrativeState) -> dict:
    buffer = state.get("volatile_buffer", {})
    proposal = buffer.get("proposal", "")
    attempt = buffer.get(
        "attempt",
        TOTAL_ALLOWED_LOOPS - state.get("remaining_loops", TOTAL_ALLOWED_LOOPS) + 1,
    )
    contradiction_reason = ""

    if not proposal:
        contradiction_reason = "Simulator produced no proposal."
    elif proposal.startswith("[GATEWAY"):
        contradiction_reason = proposal
    else:
        async with aiohttp.ClientSession(trust_env=False) as session:
            judgment = await _llm_call(
                session,
                trace_id=state["trace_id"],
                system=(
                    "You are the Continuity Checker. Compare the proposal to the canon. "
                    "Respond exactly with OK or CONTRADICTION: <reason>."
                ),
                user=f"Canon:\n{state.get('current_story_arc', '')}\n\nProposal:\n{proposal}",
                temperature=0.0,
                max_tokens=128,
            )
        if "contradiction" in judgment.lower():
            contradiction_reason = judgment.strip()

    if "contradiction" in state.get("input", "").lower():
        contradiction_reason = contradiction_reason or "Contradiction requested in user input."

    if contradiction_reason:
        remaining = max(0, state.get("remaining_loops", TOTAL_ALLOWED_LOOPS) - 1)
        retry_count = TOTAL_ALLOWED_LOOPS - remaining
        message = (
            f"[CONTINUITY] Attempt {attempt} failed: {contradiction_reason}. "
            f"remaining_loops={remaining}"
        )
        return {
            "remaining_loops": remaining,
            "retry_count": retry_count,
            "consensus_reached": False,
            "validation_errors": [message],
            "execution_log": [message],
        }

    try:
        await qdrant_client.upsert(
            collection_name=QDRANT_NARRATIVE_ALIAS,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=[0.0] * 768,
                    payload={
                        "consensus_proposal": proposal,
                        "timestamp": time.time(),
                        "attempt": attempt,
                        "content_type": state.get("intent", "narrative"),
                    },
                )
            ],
        )
    except Exception as exc:
        logger.warning("[CONTINUITY] Alias upsert failed without invalidating consensus: %s", exc)

    committed = f"[COMMITTED attempt={attempt}] {proposal}"
    return {
        "current_story_arc": proposal,
        "volatile_buffer": {},
        "final_output": proposal,
        "message_array": [committed],
        "consensus_reached": True,
        "termination_reason": "",
        "execution_log": [f"[CONTINUITY] Consensus reached on attempt {attempt}."],
    }


async def fail_safe_termination(state: NarrativeState) -> dict:
    restored = state.get("initial_pristine_payload", DEFAULT_CANON)
    reason = (
        f"Consensus was not reached after {TOTAL_ALLOWED_LOOPS} attempts; "
        "volatile runtime buffers were cleared and the pristine Lorekeeper checkpoint was restored."
    )
    message = f"[FAIL-SAFE TERMINATION] {reason}\n\n{restored}"
    log_entry = f"[FAIL-SAFE] thread_id={state.get('thread_id')} reason={reason}"
    await _persist_execution_event(
        thread_id=state.get("thread_id", ""),
        trace_id=state.get("trace_id", ""),
        event_type="fail_safe_termination",
        payload={"reason": reason, "retry_count": TOTAL_ALLOWED_LOOPS},
    )
    logger.error(log_entry)
    return {
        "volatile_buffer": {},
        "current_story_arc": restored,
        "final_output": message,
        "message_array": [message],
        "termination_reason": reason,
        "consensus_reached": False,
        "remaining_loops": 0,
        "retry_count": TOTAL_ALLOWED_LOOPS,
        "execution_log": [log_entry],
    }


def route_verifier(state: NarrativeState) -> str:
    if state.get("consensus_reached"):
        return END
    if state.get("remaining_loops", 0) <= 0:
        return "fail_safe_termination"
    return "Simulator"


def build_graph(checkpointer: AsyncPostgresSaver):
    workflow = StateGraph(NarrativeState)
    workflow.add_node("Semantic_Router_Node", Semantic_Router_Node)
    workflow.add_node("Factual_Shortcircuit_Node", Factual_Shortcircuit_Node)
    workflow.add_node("Lorekeeper_Node", Lorekeeper_Node)
    workflow.add_node("Simulator", Simulator)
    workflow.add_node("Continuity_Verifier", Continuity_Verifier)
    workflow.add_node("fail_safe_termination", fail_safe_termination)
    workflow.set_entry_point("Semantic_Router_Node")
    workflow.add_conditional_edges(
        "Semantic_Router_Node",
        route_after_semantic,
        {
            "Factual_Shortcircuit_Node": "Factual_Shortcircuit_Node",
            "Lorekeeper_Node": "Lorekeeper_Node",
        },
    )
    workflow.add_edge("Factual_Shortcircuit_Node", END)
    workflow.add_edge("Lorekeeper_Node", "Simulator")
    workflow.add_edge("Simulator", "Continuity_Verifier")
    workflow.add_conditional_edges(
        "Continuity_Verifier",
        route_verifier,
        {
            "Simulator": "Simulator",
            "fail_safe_termination": "fail_safe_termination",
            END: END,
        },
    )
    workflow.add_edge("fail_safe_termination", END)
    return workflow.compile(checkpointer=checkpointer)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _checkpointer_context, _checkpointer, _graph, _ops_pool

    await broker.connect()
    if not broker.is_connected:
        raise RuntimeError("Redis HITL broker is unavailable.")
    set_broker(broker)

    await _ensure_narrative_alias()

    _checkpointer_context = AsyncPostgresSaver.from_conn_string(
        POSTGRES_LANGGRAPH_URL,
        serde=JsonPlusSerializer(pickle_fallback=False),
    )
    _checkpointer = await _checkpointer_context.__aenter__()
    await _checkpointer.setup()
    _graph = build_graph(_checkpointer)

    _ops_pool = await asyncpg.create_pool(POSTGRES_OPS_URL, min_size=1, max_size=4)
    await _ops_pool.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_events (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            thread_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL
        )
        """
    )
    yield
    await broker.disconnect()
    await qdrant_client.close()
    await _ops_pool.close()
    await _checkpointer_context.__aexit__(None, None, None)


app = FastAPI(title="Hardened LangGraph Orchestrator", version="3.0.0", lifespan=lifespan)
app.include_router(hitl_router)


@app.get("/health")
async def health() -> JSONResponse:
    details: dict[str, Any] = {
        "qdrant_alias": False,
        "postgres_checkpointer": _checkpointer is not None,
        "postgres_ops": _ops_pool is not None,
        "redis_hitl": broker.is_connected,
        "gateway": False,
    }
    try:
        aliases = await qdrant_client.get_aliases()
        details["qdrant_alias"] = any(
            alias.alias_name == QDRANT_NARRATIVE_ALIAS for alias in aliases.aliases
        )
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.get(
                f"{INFERENCE_GATEWAY_URL}/health",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as response:
                details["gateway"] = response.status == 200
    except Exception as exc:
        details["error"] = str(exc)
    healthy = all(value is True for key, value in details.items() if key != "error")
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "details": details},
    )


def _build_initial_state(
    user_input: str,
    *,
    thread_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    messages: Optional[list[dict]] = None,
) -> NarrativeState:
    return NarrativeState(
        input=user_input,
        messages=messages or [],
        intent="",
        metadata_filter={},
        current_story_arc="",
        initial_pristine_payload="",
        volatile_buffer={},
        remaining_loops=TOTAL_ALLOWED_LOOPS,
        retry_count=0,
        consensus_reached=False,
        termination_reason="",
        validation_errors=[],
        message_array=[],
        execution_log=[],
        final_output="",
        thread_id=thread_id or str(uuid.uuid4()),
        trace_id=trace_id or str(uuid.uuid4()),
    )


class InvokeRequest(BaseModel):
    input: str
    messages: list[dict] = Field(default_factory=list)
    thread_id: Optional[str] = None
    trace_id: Optional[str] = None


def _result_payload(result: dict, thread_id: str, trace_id: str) -> dict:
    return {
        "response": result.get("final_output", ""),
        "intent": result.get("intent", ""),
        "thread_id": thread_id,
        "trace_id": trace_id,
        "retry_count": result.get("retry_count", 0),
        "remaining_loops": result.get("remaining_loops", TOTAL_ALLOWED_LOOPS),
        "consensus_reached": result.get("consensus_reached", False),
        "termination_reason": result.get("termination_reason", ""),
        "message_array": result.get("message_array", []),
        "validation_errors": result.get("validation_errors", []),
        "execution_log": result.get("execution_log", []),
    }


@app.post("/invoke")
async def invoke_graph(req: InvokeRequest):
    if _graph is None:
        return JSONResponse(status_code=503, content={"error": "Graph is not initialized."})
    thread_id = req.thread_id or str(uuid.uuid4())
    trace_id = req.trace_id or str(uuid.uuid4())
    initial_state = _build_initial_state(
        req.input, thread_id=thread_id, trace_id=trace_id, messages=req.messages
    )
    config = {"configurable": {"thread_id": thread_id}}
    task = asyncio.create_task(_graph.ainvoke(initial_state, config=config))
    _active_tasks[thread_id] = task
    try:
        result = await task
    except asyncio.CancelledError:
        return {"response": "[GRAPH INTERRUPTED]", "thread_id": thread_id, "trace_id": trace_id}
    finally:
        _active_tasks.pop(thread_id, None)

    payload = _result_payload(result, thread_id, trace_id)
    await _persist_execution_event(
        thread_id=thread_id,
        trace_id=trace_id,
        event_type="graph_complete",
        payload=payload,
    )
    return payload


@app.post("/stream")
async def stream_graph(req: InvokeRequest):
    async def event_generator() -> AsyncIterator[str]:
        if _graph is None:
            yield _sse("error", {"detail": "Graph is not initialized."})
            return
        thread_id = req.thread_id or str(uuid.uuid4())
        trace_id = req.trace_id or str(uuid.uuid4())
        initial_state = _build_initial_state(
            req.input, thread_id=thread_id, trace_id=trace_id, messages=req.messages
        )
        config = {"configurable": {"thread_id": thread_id}}
        yield _sse("graph_start", {"thread_id": thread_id, "trace_id": trace_id})
        try:
            async for event in _graph.astream_events(initial_state, config=config, version="v2"):
                if event.get("event") != "on_chain_end":
                    continue
                node = event.get("name", "")
                output = event.get("data", {}).get("output", {}) or {}
                if node == "fail_safe_termination":
                    yield _sse(
                        "fail_safe_termination",
                        {
                            "termination_reason": output.get("termination_reason", ""),
                            "message_array": output.get("message_array", []),
                            "execution_log": output.get("execution_log", []),
                        },
                    )
                elif node in {
                    "Semantic_Router_Node",
                    "Lorekeeper_Node",
                    "Simulator",
                    "Continuity_Verifier",
                    "Factual_Shortcircuit_Node",
                }:
                    yield _sse(
                        "node_end",
                        {
                            "node": node,
                            "remaining_loops": output.get("remaining_loops"),
                            "retry_count": output.get("retry_count"),
                            "execution_log": output.get("execution_log", []),
                        },
                    )
                for message in output.get("message_array", []):
                    yield _sse("graph_output", {"message": message})
            yield _sse("graph_end", {"thread_id": thread_id, "trace_id": trace_id})
        except asyncio.CancelledError:
            yield _sse("graph_end", {"thread_id": thread_id, "reason": "interrupted"})
        except Exception as exc:
            logger.exception("[STREAM] Unhandled graph stream error.")
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class InterruptRequest(BaseModel):
    thread_id: str
    new_input: str
    messages: list[dict] = Field(default_factory=list)
    trace_id: Optional[str] = None
    node_index: int = 0


@app.post("/interrupt")
async def interrupt_graph(req: InterruptRequest):
    task = _active_tasks.get(req.thread_id)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _active_tasks.pop(req.thread_id, None)
    if _checkpointer is not None:
        await _checkpointer.adelete_thread(req.thread_id)

    replacement = InvokeRequest(
        input=req.new_input,
        messages=req.messages,
        trace_id=req.trace_id,
    )
    result = await invoke_graph(replacement)
    if isinstance(result, JSONResponse):
        return result
    return {"old_thread_id": req.thread_id, "new_thread_id": result["thread_id"], **result}


@app.get("/debug/state/{thread_id}")
async def debug_state(thread_id: str):
    if _checkpointer is None:
        return JSONResponse(status_code=503, content={"error": "Checkpointer unavailable."})
    checkpoint = await _checkpointer.aget({"configurable": {"thread_id": thread_id}})
    if not checkpoint:
        return {"error": f"No checkpoint found for thread_id={thread_id!r}"}
    values = checkpoint.get("channel_values", {})
    return {
        "thread_id": thread_id,
        "retry_count": values.get("retry_count", 0),
        "remaining_loops": values.get("remaining_loops", TOTAL_ALLOWED_LOOPS),
        "volatile_buffer": values.get("volatile_buffer", {}),
        "message_array": values.get("message_array", []),
        "execution_log": values.get("execution_log", []),
        "termination_reason": values.get("termination_reason", ""),
    }


class AlertRequest(BaseModel):
    source: str = "monitor"
    detail: dict[str, Any] = Field(default_factory=dict)
    trace_id: Optional[str] = None


@app.post("/webhook/alert")
async def webhook_alert(req: AlertRequest):
    trace_id = req.trace_id or str(uuid.uuid4())
    await _persist_execution_event(
        thread_id="monitor",
        trace_id=trace_id,
        event_type=f"alert:{req.source}",
        payload=req.detail,
    )
    return {"accepted": True, "trace_id": trace_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100, log_level="info")
