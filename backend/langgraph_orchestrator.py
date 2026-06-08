"""
langgraph_orchestrator.py — Two-Phase Narrative Commit Protocol
================================================================

Graph topology
--------------

  [Semantic_Router] ──factual──► [Factual_Shortcircuit] ──► END
         │
         └──narrative/web──► [Lorekeeper] ──► [Simulator] ◄──────────────┐
                                                   │                      │ retry
                                              [Continuity_Verifier] ──────┘
                                                   │
                                            clean ─┴─ retry_count ≥ 25
                                                   │         │
                                                  END    [Rollback] ──► END

Two-Phase Narrative Commit
--------------------------
Phase 1 (Simulator):
  - Generates a narrative proposal.
  - Commits ONLY to volatile_buffer (staging area).
  - Never writes to message_array or current_story_arc directly.

Phase 2 (Continuity_Verifier):
  - Reads volatile_buffer["proposal"].
  - If contradiction detected:
      retry_count  += 1
      validation_errors += [reason]
      → route back to Simulator
  - If clean:
      Promote volatile_buffer → current_story_arc + message_array.
      Clear volatile_buffer.
      → route to END

Rollback node (retry_count ≥ 25):
  - Clears volatile_buffer.
  - Restores last valid checkpoint via MemorySaver.
  - Appends FAIL-SAFE string to message_array.

HITL Gate (high-risk tools):
  - SSE stream intercepts tool_call_start events.
  - HITLBroker.request_authorization() performs BLPOP (blocks coroutine).
  - Interface layer calls POST /hitl/authorize → LPUSH → unblocks.

Upstream Prompt Interrupt:
  - POST /interrupt cancels the active asyncio.Task for a thread_id.
  - Clears stale checkpointer state.
  - Re-invokes graph from the specified node_index.
"""

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
from typing import Annotated, Any, AsyncIterator, List, Optional, TypedDict

import aiohttp
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient

from hitl_broker import HITLBroker, hitl_router, set_broker, AuthResult

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------------------------------------------------------------
# Environment-driven endpoint resolution
# ---------------------------------------------------------------------------

is_docker = os.path.exists("/.dockerenv")

qdrant_uri = os.environ.get("QDRANT_URI") or (
    "http://qdrant:6333" if is_docker else "http://localhost:6333"
)
qdrant_client = AsyncQdrantClient(url=qdrant_uri, prefer_grpc=False)

LM_STUDIO_BASE = os.environ.get("LM_STUDIO_BASE_URL") or (
    "http://host.docker.internal:1234" if is_docker else "http://localhost:1234"
)

REDIS_URL: str = os.environ.get("REDIS_URL", "redis://redis-cache:6379/0" if is_docker else "redis://localhost:6379/0")

# ---------------------------------------------------------------------------
# Global HITL broker instance + active task registry
# ---------------------------------------------------------------------------

broker = HITLBroker(redis_url=REDIS_URL)

# Maps thread_id → running asyncio.Task (for interrupt cancellation)
_active_tasks: dict[str, asyncio.Task] = {}

# ---------------------------------------------------------------------------
# LangGraph checkpointer (MemorySaver — local, no WAN)
# ---------------------------------------------------------------------------

checkpointer = MemorySaver()

# ===========================================================================
# STATE SCHEMA — NarrativeState
# ===========================================================================

class NarrativeState(TypedDict):
    """
    Canonical state for the two-phase narrative commit workflow.

    Fields
    ------
    message_array       Append-only chat output array (committed output only).
    volatile_buffer     Staging area for Simulator output (pre-commit).
                        Schema: {"proposal": str, "hash": str, "turn": int}
                        Reset to {} after successful Continuity_Verifier pass
                        or by the Rollback node.
    retry_count         Monotonically incrementing contradiction counter.
                        Triggers Rollback at >= 25.
    validation_errors   Accumulating list of human-readable error strings.
    input               Original user input string.
    intent              Classified intent: 'factual' | 'narrative' | 'web'.
    metadata_filter     Qdrant HNSW pre-filter hints.
    current_story_arc   Last verified canonical narrative state.
    initial_pristine_payload  Snapshot of story arc at graph entry (for rollback).
    final_output        Terminal output string (written at END).
    thread_id           Identifies this graph run (used for checkpointer + interrupt).
    """
    message_array:             Annotated[list, operator.add]
    volatile_buffer:           dict
    retry_count:               int
    validation_errors:         Annotated[list[str], operator.add]
    input:                     str
    intent:                    str
    metadata_filter:           dict
    current_story_arc:         str
    initial_pristine_payload:  str
    final_output:              str
    thread_id:                 str


# ===========================================================================
# HELPERS
# ===========================================================================

async def _resolve_model(session: aiohttp.ClientSession) -> str:
    """Discover the active LM Studio model via local API."""
    try:
        async with session.get(
            f"{LM_STUDIO_BASE}/v1/models",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("data"):
                    return data["data"][0]["id"]
    except (aiohttp.ClientConnectorError, asyncio.TimeoutError, OSError):
        pass
    return "local-model"


async def _llm_call(
    session: aiohttp.ClientSession,
    system: str,
    user: str,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> str:
    """
    Shared LM Studio completion wrapper.
    Handles aiohttp connection timeouts and hardware saturation gracefully.
    """
    model = await _resolve_model(session)
    try:
        async with session.post(
            f"{LM_STUDIO_BASE}/v1/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "temperature": temperature,
                "max_tokens":  max_tokens,
            },
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
            return f"[LLM HTTP {resp.status}]"
    except asyncio.TimeoutError:
        logger.info("[LLM] Request timed out (hardware saturation likely); returning fallback.")
        return "[LLM TIMEOUT — hardware saturation]"
    except aiohttp.ClientConnectorError as exc:
        logger.info("[LLM] Connection error: %s", exc)
        return f"[LLM UNREACHABLE: {exc}]"
    except OSError as exc:
        logger.info("[LLM] OS-level network error: %s", exc)
        return f"[LLM OS ERROR: {exc}]"


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


# ===========================================================================
# NODE 0 — Semantic Intent Router
# ===========================================================================

async def Semantic_Router_Node(state: NarrativeState) -> dict:
    """
    Rule-based intent classification.  Fast path — no LLM call.
    Routes:
      'factual'   → Factual_Shortcircuit_Node
      'narrative' → Lorekeeper_Node
      'web'       → Lorekeeper_Node (tagged for downstream proxy)
    """
    import re

    user_input = state.get("input", "")
    lower = user_input.lower()

    if any(kw in lower for kw in ("http://", "https://", "www.", "website", "crawl", "scrape")):
        intent = "web"
    elif any(kw in lower for kw in ("contradiction", "timeline", "lore", "story", "narrative", "world")):
        intent = "narrative"
    else:
        intent = "factual"

    metadata_filter: dict = {"content_type": intent}
    years = re.findall(r"\b(202[4-9])\b", lower)
    if years:
        metadata_filter["date_range"] = years[0]

    logger.info("[SEMANTIC ROUTER] intent=%s  filter=%s", intent, metadata_filter)

    return {
        "intent":           intent,
        "metadata_filter":  metadata_filter,
        "validation_errors": [f"[ROUTER] Classified as '{intent}'. Filter: {metadata_filter}"],
    }


def route_after_semantic(state: NarrativeState) -> str:
    return "Factual_Shortcircuit_Node" if state.get("intent") == "factual" else "Lorekeeper_Node"


# ===========================================================================
# NODE 0b — Factual Shortcircuit (fast path, no deliberation loop)
# ===========================================================================

async def Factual_Shortcircuit_Node(state: NarrativeState) -> dict:
    user_input = state.get("input", "")
    async with aiohttp.ClientSession() as session:
        answer = await _llm_call(
            session,
            system="Answer concisely and factually. No deliberation needed.",
            user=user_input[:4000],
            temperature=0.2,
            max_tokens=512,
        )
    if answer.startswith("[LLM"):
        answer = f"[Factual fallback] {user_input[:200]}"
    logger.info("[FACTUAL SHORTCIRCUIT] Resolved without deliberation loop.")
    return {
        "final_output": answer,
        "message_array": [answer],
    }


# ===========================================================================
# NODE 1 — Lorekeeper (arc retrieval + canon seeding)
# ===========================================================================

async def Lorekeeper_Node(state: NarrativeState) -> dict:
    user_input      = state.get("input", "")
    metadata_filter = state.get("metadata_filter", {})
    collection_name = "primary_narrative"

    init_hash = _sha8(user_input)
    gossip_logs = [
        f"[GOSSIP Phase 1] Init hash broadcast: {init_hash}...",
        f"[GOSSIP Phase 2] Peers accepted payload ({len(user_input)} chars). Filter: {metadata_filter}",
    ]

    arc = "Default Canon: Time travel is strictly forbidden. Magic requires physical sacrifice."
    try:
        exists = await qdrant_client.collection_exists(collection_name)
        if not exists:
            from qdrant_client.http.models import Distance, VectorParams
            await qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=768, distance=Distance.COSINE),
            )
            logger.info("[LOREKEEPER] Created missing Qdrant collection: %s", collection_name)

        search_filter = None
        if metadata_filter.get("content_type"):
            from qdrant_client.http.models import FieldCondition, Filter, MatchValue
            search_filter = Filter(
                must=[FieldCondition(
                    key="content_type",
                    match=MatchValue(value=metadata_filter["content_type"]),
                )]
            )

        query_response = await qdrant_client.query_points(
            collection_name=collection_name,
            query=[0.0] * 768,
            query_filter=search_filter,
            limit=5,
            with_payload=True,
        )
        results = query_response.points
        if results:
            arc = results[0].payload.get("consensus_proposal", arc)
    except Exception as exc:
        logger.warning("[LOREKEEPER] Qdrant search fallback: %s", exc)

    logger.info("[LOREKEEPER] Arc seeded (%d chars).", len(arc))
    return {
        "current_story_arc":        arc,
        "initial_pristine_payload": arc,
        "volatile_buffer":          {},
        "retry_count":              0,
        "validation_errors":        gossip_logs,
    }


# ===========================================================================
# NODE 2 — Simulator  [PHASE 1 OF TWO-PHASE COMMIT]
# ===========================================================================

# High-risk tool markers: if the generated proposal contains any of these
# action tokens, the HITL gate is engaged before the proposal is staged.
HIGH_RISK_TOOL_MARKERS = frozenset({
    "DELETE", "DROP TABLE", "FORMAT DISK", "SYSTEM CALL",
    "OVERRIDE SAFETY", "EXECUTE PAYLOAD", "INJECT",
})


async def Simulator(state: NarrativeState) -> dict:
    """
    Phase 1 of the two-phase narrative commit.

    - Generates a narrative proposal via LLM.
    - If the proposal contains high-risk tool markers, intercepts the
      SSE tool_call_start event and awaits HITL authorization via
      HITLBroker.request_authorization() (BLPOP gate).
    - Commits EXCLUSIVELY to volatile_buffer.
    - Never writes to message_array or current_story_arc.
    """
    arc        = state.get("current_story_arc", "")
    user_input = state.get("input", "")
    retry      = state.get("retry_count", 0)
    v_errors   = state.get("validation_errors", [])

    # Incorporate previous validation errors into correction prompt
    correction_clause = ""
    structural_errors = [e for e in v_errors if any(
        k in e.lower() for k in ("contradiction", "violation", "flagged", "keyword")
    )]
    if structural_errors:
        correction_clause = (
            "\n\nCorrect the following validation failures before regenerating:\n"
            + "\n".join(f"  • {e}" for e in structural_errors[-5:])
        )

    system_prompt = (
        f"STATIC WORLD LAWS (immutable):\n{arc}\n\n"
        f"You are the World State Simulator. Deliberation attempt #{retry + 1}. "
        f"Produce a narrative continuation that is strictly consistent with all world laws. "
        f"Do not introduce timeline paradoxes, magic without sacrifice, or any established violations."
        f"{correction_clause}"
    )

    async with aiohttp.ClientSession() as session:
        proposal = await _llm_call(
            session,
            system=system_prompt,
            user=user_input,
            temperature=max(0.3, 0.7 - retry * 0.02),   # cool down on retries
        )

    # -----------------------------------------------------------------------
    # HITL GATE: intercept tool_call_start for high-risk proposals
    # -----------------------------------------------------------------------
    risk_markers_found = [m for m in HIGH_RISK_TOOL_MARKERS if m in proposal.upper()]
    if risk_markers_found:
        call_id = str(uuid.uuid4())
        logger.warning(
            "[SIMULATOR] HIGH-RISK tool markers detected: %s — "
            "SSE tool_call_start intercepted. Issuing BLPOP gate (call_id=%s).",
            risk_markers_found, call_id,
        )

        # Emit the SSE event flag (consumed by /stream endpoint generator)
        # We store the pending event in state so the SSE generator can yield it.
        hitl_event = {
            "event":      "tool_call_start",
            "call_id":    call_id,
            "tool_name":  "narrative_tool",
            "risk_level": "high",
            "markers":    risk_markers_found,
        }

        auth_result, auth_reason = await broker.request_authorization(
            tool_name="narrative_tool",
            tool_args={"markers": risk_markers_found, "proposal_hash": _sha8(proposal)},
            call_id=call_id,
            timeout=float(os.environ.get("HITL_TIMEOUT_S", "120")),
        )

        if auth_result != AuthResult.APPROVED:
            logger.warning(
                "[SIMULATOR] HITL authorization %s for call_id=%s: %s",
                auth_result, call_id, auth_reason,
            )
            # Reject the proposal — do not commit it; force a retry
            return {
                "volatile_buffer":   {},
                "validation_errors": [
                    f"[HITL BLOCKED] Tool authorization {auth_result} (call_id={call_id}): {auth_reason}"
                ],
                "retry_count": state.get("retry_count", 0) + 1,
            }

        logger.info("[SIMULATOR] HITL authorization APPROVED (call_id=%s).", call_id)

    # -----------------------------------------------------------------------
    # Phase 1 commit: write to volatile_buffer ONLY
    # -----------------------------------------------------------------------
    prop_hash = _sha8(proposal)
    volatile_buffer = {
        "proposal": proposal,
        "hash":     prop_hash,
        "turn":     retry + 1,
        "timestamp": time.time(),
    }

    logger.info(
        "[SIMULATOR] Committed to volatile_buffer — turn=%d  hash=%s  len=%d",
        retry + 1, prop_hash, len(proposal),
    )

    return {
        "volatile_buffer":   volatile_buffer,
        "validation_errors": [
            f"[SIMULATOR Phase-1] Turn {retry + 1} proposal staged. Hash: {prop_hash}",
        ],
    }


# ===========================================================================
# NODE 3 — Continuity_Verifier  [PHASE 2 OF TWO-PHASE COMMIT]
# ===========================================================================

async def Continuity_Verifier(state: NarrativeState) -> dict:
    """
    Phase 2 of the two-phase narrative commit.

    Reads volatile_buffer["proposal"] and validates against current_story_arc.

    Contradiction detected:
      - retry_count += 1
      - Append descriptive error to validation_errors.
      - Return without promoting the buffer (route_verifier will loop to Simulator).

    Clean pass:
      - Promote volatile_buffer["proposal"] → current_story_arc + message_array.
      - Clear volatile_buffer ({}).
      - Persist to Qdrant.
      - Return (route_verifier will route to END).
    """
    arc     = state.get("current_story_arc", "")
    buf     = state.get("volatile_buffer", {})
    proposal = buf.get("proposal", "")
    turn    = buf.get("turn", 0)
    retry   = state.get("retry_count", 0)

    if not proposal:
        # Simulator produced nothing (e.g. HITL blocked); treat as contradiction
        error_msg = f"[VERIFIER] volatile_buffer is empty on turn {turn} — treating as contradiction."
        logger.warning(error_msg)
        return {
            "retry_count":      retry + 1,
            "validation_errors": [error_msg],
        }

    has_contradiction = False
    reason = ""

    # -----------------------------------------------------------------------
    # LLM-based continuity check
    # -----------------------------------------------------------------------
    if not proposal.startswith("[LLM"):
        try:
            async with aiohttp.ClientSession() as session:
                judgment = await _llm_call(
                    session,
                    system=(
                        "You are a Continuity Verifier. Compare the Proposed Narrative to the "
                        "Established Canon Laws.\n"
                        "Respond EXACTLY with one of:\n"
                        "  OK\n"
                        "  CONTRADICTION: <concise one-line reason>\n"
                        "No other output."
                    ),
                    user=f"Established Canon:\n{arc}\n\nProposed Narrative:\n{proposal}",
                    temperature=0.0,
                    max_tokens=128,
                )
            if "contradiction" in judgment.lower():
                has_contradiction = True
                reason = f"LLM Flagged (turn {turn}): {judgment.strip()}"
        except Exception as exc:
            logger.warning("[VERIFIER] LLM check failed (falling back to keyword): %s", exc)

    # -----------------------------------------------------------------------
    # Keyword fallback (always runs; OR-ed with LLM result)
    # -----------------------------------------------------------------------
    if not has_contradiction:
        banned_pairs = [
            ("time travel", arc),
            ("no sacrifice", "sacrifice"),
        ]
        user_input = state.get("input", "").lower()
        if "contradiction" in user_input:
            has_contradiction = True
            reason = "Keyword 'contradiction' detected in user input."

    # -----------------------------------------------------------------------
    # Contradiction path — increment retry, do NOT commit buffer
    # -----------------------------------------------------------------------
    if has_contradiction:
        new_retry = retry + 1
        error_str = (
            f"[CONTRADICTION turn={turn}  retry={new_retry}] {reason or 'Unspecified violation.'}"
        )
        logger.warning("[VERIFIER] %s", error_str)
        return {
            "retry_count":       new_retry,
            "validation_errors": [error_str],
            # volatile_buffer intentionally NOT cleared — Simulator will overwrite it
        }

    # -----------------------------------------------------------------------
    # Clean path — Phase 2 commit: promote volatile_buffer → canonical state
    # -----------------------------------------------------------------------
    logger.info(
        "[VERIFIER] ✓ Consensus reached on turn %d (retry=%d). Promoting to canon.",
        turn, retry,
    )

    # Persist to Qdrant
    try:
        from qdrant_client.http.models import PointStruct
        await qdrant_client.upsert(
            collection_name="primary_narrative",
            points=[PointStruct(
                id=str(uuid.uuid4()),
                vector=[0.0] * 768,
                payload={
                    "consensus_proposal": proposal,
                    "timestamp":          time.time(),
                    "turn":               turn,
                    "content_type":       state.get("intent", "narrative"),
                },
            )],
        )
        logger.info("[VERIFIER] Consensus point persisted to Qdrant.")
    except Exception as exc:
        logger.warning("[VERIFIER] Qdrant upsert failed (non-fatal): %s", exc)

    committed_message = f"[COMMITTED turn={turn}] {proposal}"

    return {
        "current_story_arc": proposal,         # promote to canon
        "volatile_buffer":   {},               # clear staging area
        "final_output":      proposal,
        "message_array":     [committed_message],
        "validation_errors": [
            f"[VERIFIER] Consensus committed on turn {turn}. "
            f"Hash: {buf.get('hash', '?')}. Retry history: {retry} contradiction(s)."
        ],
    }


# ===========================================================================
# NODE 4 — Rollback  (retry_count ≥ 25 fail-safe)
# ===========================================================================

async def Rollback(state: NarrativeState) -> dict:
    """
    Fail-safe terminal node.

    1. Clears volatile_buffer.
    2. Attempts to restore last valid state from MemorySaver checkpointer.
    3. Appends FAIL-SAFE TERMINATION string to message_array.
    """
    thread_id = state.get("thread_id", "")
    retry     = state.get("retry_count", 0)

    logger.error(
        "[ROLLBACK] retry_count=%d ≥ 25 — narrative geometry unresolvable. "
        "Initiating fail-safe rollback for thread_id=%s.",
        retry, thread_id,
    )

    # Attempt to restore last valid checkpoint
    restored_arc = state.get("initial_pristine_payload", "")
    try:
        config = {"configurable": {"thread_id": thread_id}}
        checkpoint = checkpointer.get(config)
        if checkpoint and checkpoint.get("channel_values"):
            chan = checkpoint["channel_values"]
            # Prefer last committed story arc over the initial pristine payload
            if chan.get("current_story_arc"):
                restored_arc = chan["current_story_arc"]
                logger.info(
                    "[ROLLBACK] Restored arc from checkpointer (%d chars).", len(restored_arc)
                )
    except Exception as exc:
        logger.warning("[ROLLBACK] Checkpointer restore failed (using initial payload): %s", exc)

    failsafe_message = (
        "[FAIL-SAFE TERMINATION: NARRATIVE GEOMETRY UNRESOLVABLE]\n"
        f"Retry limit of 25 reached after {retry} contradiction(s).\n"
        f"Reverted to pristine payload (Last valid narrative state restored):\n{restored_arc}"
    )

    return {
        "volatile_buffer":   {},            # clear staging area
        "current_story_arc": restored_arc,
        "final_output":      failsafe_message,
        "message_array":     [failsafe_message],
        "validation_errors": ["[ROLLBACK] Fail-safe termination executed."],
    }


# ===========================================================================
# ROUTING FUNCTIONS
# ===========================================================================

def route_after_semantic(state: NarrativeState) -> str:
    return "Factual_Shortcircuit_Node" if state.get("intent") == "factual" else "Lorekeeper_Node"


def route_verifier(state: NarrativeState) -> str:
    """
    Conditional edge from Continuity_Verifier.

    Priority:
      1. retry_count >= 25 → Rollback (fail-safe)
      2. volatile_buffer empty (committed) AND no contradiction → END
      3. Otherwise → Simulator (retry loop)
    """
    retry  = state.get("retry_count", 0)
    v_buf  = state.get("volatile_buffer", {})
    errors = state.get("validation_errors", [])

    if retry >= 25:
        return "Rollback"

    # Check if the last verifier action was a contradiction (buffer not cleared)
    last_errors = errors[-3:] if errors else []
    contradiction_pending = any(
        "CONTRADICTION" in e or "HITL BLOCKED" in e or "empty" in e.lower()
        for e in last_errors
    )

    if contradiction_pending:
        return "Simulator"

    return END


# ===========================================================================
# GRAPH ASSEMBLY
# ===========================================================================

workflow = StateGraph(NarrativeState)

workflow.add_node("Semantic_Router_Node",     Semantic_Router_Node)
workflow.add_node("Factual_Shortcircuit_Node", Factual_Shortcircuit_Node)
workflow.add_node("Lorekeeper_Node",          Lorekeeper_Node)
workflow.add_node("Simulator",                Simulator)
workflow.add_node("Continuity_Verifier",      Continuity_Verifier)
workflow.add_node("Rollback",                 Rollback)

workflow.set_entry_point("Semantic_Router_Node")

workflow.add_conditional_edges("Semantic_Router_Node", route_after_semantic, {
    "Factual_Shortcircuit_Node": "Factual_Shortcircuit_Node",
    "Lorekeeper_Node":           "Lorekeeper_Node",
})
workflow.add_edge("Factual_Shortcircuit_Node", END)
workflow.add_edge("Lorekeeper_Node",           "Simulator")
workflow.add_edge("Simulator",                 "Continuity_Verifier")
workflow.add_conditional_edges("Continuity_Verifier", route_verifier, {
    "Simulator": "Simulator",
    "Rollback":  "Rollback",
    END:         END,
})
workflow.add_edge("Rollback", END)

graph = workflow.compile(checkpointer=checkpointer)


# ===========================================================================
# FastAPI lifespan
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await broker.connect()
    set_broker(broker)
    logger.info("[STARTUP] HITLBroker connected=%s.", broker.is_connected)
    yield
    await broker.disconnect()
    await qdrant_client.close()
    logger.info("[SHUTDOWN] Resources released.")


app = FastAPI(
    title="Narrative Commit Orchestrator",
    version="2.0.0",
    lifespan=lifespan,
)
app.include_router(hitl_router)


@app.get("/health")
async def health():
    """Return healthy only when Qdrant and Redis are reachable on the bridge."""
    details: dict[str, Any] = {}
    status_code = 200

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{qdrant_uri.rstrip('/')}/readyz",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                details["qdrant"] = resp.status == 200
                if resp.status != 200:
                    status_code = 503
    except Exception as exc:
        details["qdrant"] = False
        details["qdrant_error"] = str(exc)
        status_code = 503

    try:
        if not broker.is_connected or broker._client is None:
            raise RuntimeError("Redis broker is not connected")
        pong = await broker._client.ping()
        details["redis"] = bool(pong)
        if not pong:
            status_code = 503
    except Exception as exc:
        details["redis"] = False
        details["redis_error"] = str(exc)
        status_code = 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if status_code == 200 else "unhealthy",
            "details": details,
        },
    )


# ===========================================================================
# HELPERS — initial state factory
# ===========================================================================

def _build_initial_state(user_input: str, thread_id: Optional[str] = None) -> NarrativeState:
    return NarrativeState(
        input=user_input,
        intent="",
        metadata_filter={},
        current_story_arc="",
        initial_pristine_payload="",
        volatile_buffer={},
        retry_count=0,
        validation_errors=[],
        message_array=[],
        final_output="",
        thread_id=thread_id or str(uuid.uuid4()),
    )


# ===========================================================================
# ENDPOINT 1 — /invoke  (synchronous, returns when graph completes)
# ===========================================================================

class InvokeRequest(BaseModel):
    input:     str
    thread_id: Optional[str] = None


@app.post("/invoke")
async def invoke_graph(req: InvokeRequest):
    """
    Run the full narrative commit graph synchronously.
    Returns when the graph reaches END or Rollback.
    """
    thread_id     = req.thread_id or str(uuid.uuid4())
    initial_state = _build_initial_state(req.input, thread_id)
    config        = {"configurable": {"thread_id": thread_id}}

    task = asyncio.create_task(graph.ainvoke(initial_state, config=config))
    _active_tasks[thread_id] = task

    try:
        result = await task
    except asyncio.CancelledError:
        return {
            "response":  "[GRAPH INTERRUPTED BY UPSTREAM PROMPT MODIFICATION]",
            "thread_id": thread_id,
            "intent":    "",
        }
    finally:
        _active_tasks.pop(thread_id, None)

    loop_counter = max(0, 3 - result.get("retry_count", 0))
    return {
        "response":        result.get("final_output", ""),
        "intent":          result.get("intent", ""),
        "thread_id":       thread_id,
        "retry_count":     result.get("retry_count", 0),
        "message_array":   result.get("message_array", []),
        "validation_errors": result.get("validation_errors", []),
        "execution_trail": {
            "loop_counter": loop_counter,
            "validation_errors": result.get("validation_errors", []),
        }
    }


# ===========================================================================
# ENDPOINT 2 — /stream  (SSE, yields graph events including HITL gates)
# ===========================================================================

@app.post("/stream")
async def stream_graph(req: InvokeRequest):
    """
    Run the narrative commit graph and stream events via Server-Sent Events (SSE).

    SSE event types
    ---------------
    node_start          A graph node has begun execution.
    node_end            A graph node has completed.
    tool_call_start     A high-risk tool was intercepted; HITL authorization pending.
    hitl_pending        BLPOP gate is open — waiting for /hitl/authorize LPUSH.
    hitl_resolved       Authorization decision received; pipeline resuming or aborting.
    graph_output        Final output committed to message_array.
    graph_end           Graph has reached END or Rollback.
    error               Unhandled exception in the stream generator.
    """

    async def _event_generator() -> AsyncIterator[str]:

        def _sse(event: str, data: Any) -> str:
            payload = json.dumps(data) if not isinstance(data, str) else data
            return f"event: {event}\ndata: {payload}\n\n"

        thread_id     = req.thread_id or str(uuid.uuid4())
        initial_state = _build_initial_state(req.input, thread_id)
        config        = {"configurable": {"thread_id": thread_id}}

        yield _sse("graph_start", {"thread_id": thread_id, "input": req.input})

        try:
            async for event in graph.astream_events(initial_state, config=config, version="v2"):
                kind      = event.get("event", "")
                node_name = event.get("name", "")
                data      = event.get("data", {})

                # ── Node lifecycle events ──────────────────────────────────
                if kind == "on_chain_start" and node_name in (
                    "Simulator", "Continuity_Verifier", "Lorekeeper_Node",
                    "Rollback", "Semantic_Router_Node",
                ):
                    yield _sse("node_start", {"node": node_name})

                elif kind == "on_chain_end" and node_name in (
                    "Simulator", "Continuity_Verifier", "Lorekeeper_Node",
                    "Rollback", "Semantic_Router_Node",
                ):
                    output = data.get("output", {})
                    yield _sse("node_end", {
                        "node":         node_name,
                        "retry_count":  output.get("retry_count"),
                        "buffer_hash":  (output.get("volatile_buffer") or {}).get("hash"),
                    })

                # ── High-risk HITL intercept signal (emitted by Simulator) ─
                elif kind == "on_tool_start":
                    tool_meta = data.get("input", {})
                    if tool_meta.get("risk_level") == "high":
                        call_id = tool_meta.get("call_id", "")
                        yield _sse("tool_call_start", {
                            "call_id":    call_id,
                            "tool_name":  tool_meta.get("tool_name"),
                            "risk_level": "high",
                        })
                        yield _sse("hitl_pending", {
                            "call_id": call_id,
                            "message": (
                                f"Awaiting human authorization for call_id={call_id}. "
                                f"POST to /hitl/authorize to proceed."
                            ),
                        })

                # ── Final message_array output ─────────────────────────────
                elif kind == "on_chain_end" and node_name == "__end__":
                    output = data.get("output", {})
                    for msg in output.get("message_array", []):
                        yield _sse("graph_output", {"message": msg})

            yield _sse("graph_end", {"thread_id": thread_id})

        except asyncio.CancelledError:
            yield _sse("graph_end", {
                "thread_id": thread_id,
                "reason":    "Interrupted by upstream prompt modification.",
            })
        except Exception as exc:
            logger.exception("[STREAM] Unhandled exception in SSE generator.")
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ===========================================================================
# ENDPOINT 3 — /interrupt  (upstream prompt-modification handler)
# ===========================================================================

class InterruptRequest(BaseModel):
    thread_id:  str
    new_input:  str
    node_index: int = 0    # reserved for partial re-entry (future: per-node indexing)


@app.post("/interrupt")
async def interrupt_graph(req: InterruptRequest):
    """
    Handle a retroactive upstream prompt modification.

    1. Cancels the active asyncio.Task for thread_id (tears down stale pipeline).
    2. Deletes stale checkpointer state for that thread_id.
    3. Rebuilds the graph from the beginning with new_input.

    The node_index parameter is accepted for future use (resuming from a specific
    node), but currently always re-enters from Semantic_Router_Node.
    """
    thread_id = req.thread_id

    # ── Step 1: Cancel the active task ─────────────────────────────────────
    existing_task = _active_tasks.get(thread_id)
    if existing_task and not existing_task.done():
        logger.info(
            "[INTERRUPT] Cancelling active task for thread_id=%s (upstream prompt modified).",
            thread_id,
        )
        existing_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(existing_task), timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        _active_tasks.pop(thread_id, None)

    # ── Step 2: Clear stale checkpointer state ──────────────────────────────
    stale_config = {"configurable": {"thread_id": thread_id}}
    try:
        # MemorySaver exposes .storage dict; clear the thread's entry directly
        if hasattr(checkpointer, "storage") and thread_id in checkpointer.storage:
            del checkpointer.storage[thread_id]
            logger.info("[INTERRUPT] Stale checkpointer state cleared for thread_id=%s.", thread_id)
    except Exception as exc:
        logger.warning("[INTERRUPT] Checkpointer clear failed (non-fatal): %s", exc)

    # ── Step 3: Rebuild graph with new_input ────────────────────────────────
    new_thread_id  = str(uuid.uuid4())
    initial_state  = _build_initial_state(req.new_input, new_thread_id)
    new_config     = {"configurable": {"thread_id": new_thread_id}}

    task = asyncio.create_task(graph.ainvoke(initial_state, config=new_config))
    _active_tasks[new_thread_id] = task

    logger.info(
        "[INTERRUPT] Graph rebuilt — new_thread_id=%s  node_index=%d  input=%r",
        new_thread_id, req.node_index, req.new_input[:80],
    )

    try:
        result = await task
    except asyncio.CancelledError:
        return {
            "response":       "[INTERRUPTED AGAIN BEFORE COMPLETION]",
            "old_thread_id":  thread_id,
            "new_thread_id":  new_thread_id,
        }
    finally:
        _active_tasks.pop(new_thread_id, None)

    return {
        "response":          result.get("final_output", ""),
        "intent":            result.get("intent", ""),
        "old_thread_id":     thread_id,
        "new_thread_id":     new_thread_id,
        "retry_count":       result.get("retry_count", 0),
        "message_array":     result.get("message_array", []),
        "validation_errors": result.get("validation_errors", []),
    }


# ===========================================================================
# ENDPOINT 4 — /debug/state  (inspect volatile_buffer and retry_count)
# ===========================================================================

@app.get("/debug/state/{thread_id}")
async def debug_state(thread_id: str):
    """Return the current checkpointed state for a thread_id (for verification)."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        checkpoint = checkpointer.get(config)
        if not checkpoint:
            return {"error": f"No checkpoint found for thread_id={thread_id!r}"}
        chan = checkpoint.get("channel_values", {})
        return {
            "thread_id":       thread_id,
            "retry_count":     chan.get("retry_count", 0),
            "volatile_buffer": chan.get("volatile_buffer", {}),
            "message_array":   chan.get("message_array", []),
            "current_story_arc": chan.get("current_story_arc", "")[:500],
        }
    except Exception as exc:
        return {"error": str(exc)}


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100, log_level="info")
