"""
monitor_daemon.py — CPU-Bound Web Change & Semantic Drift Monitor
==================================================================

Architecture
------------
  FastAPI
    └── POST /monitor/evaluate        ← primary evaluation endpoint
    └── GET  /monitor/status          ← daemon health check
    └── GET  /monitor/history         ← recent evaluation log

Core pipeline (per evaluation cycle)
-------------------------------------
  1. Invoke deep-web-mcp  POST /extract/stream (SSE) or tool call
     → receive sanitized Markdown payload
  2. Embed payload strictly on CPU threads (all-MiniLM-L6-v2 via
     sentence-transformers with device="cpu")
  3. Query the configured Qdrant alias for the previously stored vector
     keyed by SHA-256(url)
  4. Compute cosine distance between new and stored vectors
  5a. Distance < DRIFT_THRESHOLD → no action, log unchanged
  5b. Distance ≥ DRIFT_THRESHOLD → update Qdrant, POST alert to LangGraph
  6. 404 from Qdrant → baseline init: store vector, log [BASELINE ESTABLISHED]

Edge-case contracts
-------------------
  EGRESS_TIMEOUT_BREACH from deep-web-mcp → log WARNING, exit cycle silently
  Qdrant 404                              → baseline mode (no LLM wake)
  CPU embedding thread count              → bounded by CPU_THREADS env var

Design constraints
------------------
  ✗ No GPU / CUDA calls — device="cpu" enforced at model load time
  ✗ No WAN calls — all traffic via Docker internal network aliases
  ✗ No infinite retry loops — every error path terminates the cycle
  ✓ asyncio.to_thread() for CPU-bound embedding (never blocks event loop)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

import aiohttp
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("monitor_daemon")

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

# Qdrant REST endpoint — uses Docker internal alias
QDRANT_URL: str     = os.getenv("QDRANT_URL",     "http://qdrant:6333")
COLLECTION: str     = os.getenv("QDRANT_COLLECTION", "monitor_active")

# deep-web-mcp service endpoint
MCP_URL: str        = os.getenv("MCP_URL",         "http://deep-web-mcp:8000")

# LangGraph orchestrator alert webhook
ORCHESTRATOR_URL: str = os.getenv(
    "ORCHESTRATOR_URL",
    "http://langgraph-orchestrator:8100",
)
ALERT_ENDPOINT: str = f"{ORCHESTRATOR_URL}/webhook/alert"

# Cosine DISTANCE threshold (0 = identical, 1 = orthogonal, 2 = opposite)
# 0.15 ≈ moderate semantic drift (~77° in vector space)
DRIFT_THRESHOLD: float = float(os.getenv("DRIFT_THRESHOLD", "0.15"))

# Sentence-transformer model — CPU-only, no GPU dependency
EMBED_MODEL_NAME: str = os.getenv(
    "EMBED_MODEL_NAME",
    "sentence-transformers/all-MiniLM-L6-v2",
)

# CPU thread count for intra-op parallelism (set explicitly to prevent
# PyTorch from stealing threads from the primary inference model)
CPU_THREADS: int = int(os.getenv("CPU_THREADS", "4"))

# Timeout values
MCP_TIMEOUT_S:    float = float(os.getenv("MCP_TIMEOUT_S",    "90.0"))
QDRANT_TIMEOUT_S: float = float(os.getenv("QDRANT_TIMEOUT_S", "10.0"))
ALERT_TIMEOUT_S:  float = float(os.getenv("ALERT_TIMEOUT_S",  "30.0"))

# ---------------------------------------------------------------------------
# CPU embedding model (lazy-loaded singleton)
# ---------------------------------------------------------------------------

_embed_model = None          # sentence_transformers.SentenceTransformer
_embed_lock  = asyncio.Lock()


async def _get_embed_model():
    """
    Lazy-load the SentenceTransformer model exactly once.

    device="cpu" is enforced unconditionally — this daemon must never
    allocate GPU VRAM or consume PCIe bandwidth from the primary model.

    CPU_THREADS bounds PyTorch's intra-op thread pool so the daemon
    does not steal cores from the LM Studio inference process.
    """
    global _embed_model
    if _embed_model is not None:
        return _embed_model

    async with _embed_lock:
        if _embed_model is not None:      # double-checked locking
            return _embed_model

        def _load() -> "SentenceTransformer":
            import torch
            from sentence_transformers import SentenceTransformer

            # Hard-cap CPU thread count before the model is instantiated
            torch.set_num_threads(CPU_THREADS)
            torch.set_num_interop_threads(max(1, CPU_THREADS // 2))

            logger.info(
                "[EMBED] Loading %r on CPU (threads=%d)…",
                EMBED_MODEL_NAME, CPU_THREADS,
            )
            model = SentenceTransformer(EMBED_MODEL_NAME, device="cpu")
            logger.info("[EMBED] Model loaded — dim=%d", model.get_sentence_embedding_dimension())
            return model

        # Run blocking model load in a thread pool without blocking the event loop
        _embed_model = await asyncio.to_thread(_load)
    return _embed_model


async def _embed_text(text: str) -> list[float]:
    """
    Generate a dense CPU embedding for *text*.

    Runs sentence_transformers encode() inside asyncio.to_thread() so the
    CPU-bound computation never blocks the FastAPI event loop.

    Returns a plain Python list of floats (Qdrant payload format).
    """
    model = await _get_embed_model()

    def _encode() -> list[float]:
        vec = model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True,   # pre-normalise → cosine == dot product
            show_progress_bar=False,
        )
        return vec.tolist()

    return await asyncio.to_thread(_encode)


# ---------------------------------------------------------------------------
# Evaluation history ring buffer (in-memory, last 100 entries)
# ---------------------------------------------------------------------------

@dataclass
class EvalRecord:
    eval_id:    str
    url:        str
    timestamp:  float
    outcome:    str       # baseline | unchanged | drift_detected | error
    distance:   Optional[float] = None
    error_code: Optional[str]   = None

_eval_history: list[EvalRecord] = []
_HISTORY_MAX:  int = 100


@dataclass
class OperationStep:
    name: str
    label: str
    status: str = "pending"  # pending | running | success | error | skipped
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    detail: Optional[str] = None


@dataclass
class OperationRecord:
    eval_id: str
    url: str
    started_at: float
    updated_at: float
    status: str = "running"
    outcome: Optional[str] = None
    current_step: Optional[str] = None
    steps: list[OperationStep] = field(default_factory=list)


_PIPELINE_STEPS = (
    ("extraction", "Deep-web extraction"),
    ("embedding", "CPU embedding"),
    ("alias_check", "Qdrant alias check"),
    ("vector_lookup", "Baseline lookup"),
    ("distance", "Drift calculation"),
    ("vector_write", "Vector persistence"),
    ("alert", "Orchestrator alert"),
)
_active_operations: dict[str, OperationRecord] = {}
_recent_operations: list[OperationRecord] = []
_OPERATIONS_MAX = 20
_started_at = time.time()


def _start_operation(eval_id: str, url: str) -> None:
    now = time.time()
    _active_operations[eval_id] = OperationRecord(
        eval_id=eval_id,
        url=url,
        started_at=now,
        updated_at=now,
        steps=[OperationStep(name=name, label=label) for name, label in _PIPELINE_STEPS],
    )


def _set_operation_step(eval_id: str, step_name: str, detail: Optional[str] = None) -> None:
    operation = _active_operations.get(eval_id)
    if operation is None:
        return
    now = time.time()
    for step in operation.steps:
        if step.status == "running":
            step.status = "success"
            step.completed_at = now
        if step.name == step_name:
            step.status = "running"
            step.started_at = step.started_at or now
            step.detail = detail
    operation.current_step = step_name
    operation.updated_at = now


def _finish_operation(record: EvalRecord) -> None:
    operation = _active_operations.pop(record.eval_id, None)
    if operation is None:
        return
    now = time.time()
    operation.updated_at = now
    operation.outcome = record.outcome
    operation.status = "error" if record.outcome == "error" else "success"
    for step in operation.steps:
        if step.status == "running":
            step.status = operation.status
            step.completed_at = now
            step.detail = record.error_code or step.detail
        elif step.status == "pending":
            step.status = "skipped"
    _recent_operations.insert(0, operation)
    del _recent_operations[_OPERATIONS_MAX:]


def _record_eval(record: EvalRecord) -> None:
    _eval_history.append(record)
    if len(_eval_history) > _HISTORY_MAX:
        _eval_history.pop(0)
    _finish_operation(record)


# ---------------------------------------------------------------------------
# Cosine distance helper
# ---------------------------------------------------------------------------

def _cosine_distance(a: list[float], b: list[float]) -> float:
    """
    Compute cosine distance between two L2-normalised vectors.

    distance = 1 - dot(a, b)

    Because both vectors are pre-normalised by all-MiniLM-L6-v2,
    the dot product equals the cosine similarity directly.
    Range: 0.0 (identical) → 1.0 (orthogonal) → 2.0 (opposite).
    """
    if len(a) != len(b):
        raise ValueError(
            f"Vector dimension mismatch: a={len(a)}, b={len(b)}"
        )
    dot = sum(x * y for x, y in zip(a, b))
    return 1.0 - dot


# ---------------------------------------------------------------------------
# Qdrant helpers
# ---------------------------------------------------------------------------

def _url_point_id(url: str) -> str:
    """
    Derive a deterministic Qdrant point ID from the target URL.
    Qdrant accepts string UUIDs or unsigned integers; we use a UUID5
    namespace-keyed on the URL so the same URL always maps to the same ID.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, url))


async def _qdrant_lookup(
    session:  aiohttp.ClientSession,
    point_id: str,
) -> Optional[dict]:
    """
    Retrieve a Qdrant point by ID from COLLECTION.

    Returns the point dict if found, None if 404, raises on other errors.
    """
    url = f"{QDRANT_URL}/collections/{COLLECTION}/points/{point_id}"
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=QDRANT_TIMEOUT_S),
    ) as resp:
        if resp.status == 404:
            return None
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(
                f"Qdrant GET point returned HTTP {resp.status}: {body[:200]}"
            )
        data = await resp.json(content_type=None)
        return data.get("result")


async def _qdrant_upsert(
    session:  aiohttp.ClientSession,
    point_id: str,
    vector:   list[float],
    payload:  dict,
) -> None:
    """
    Upsert (insert or update) a single vector point into COLLECTION.
    """
    body = {
        "points": [
            {
                "id":      point_id,
                "vector":  vector,
                "payload": payload,
            }
        ]
    }
    async with session.put(
        f"{QDRANT_URL}/collections/{COLLECTION}/points",
        json=body,
        timeout=aiohttp.ClientTimeout(total=QDRANT_TIMEOUT_S),
    ) as resp:
        if resp.status not in (200, 201, 206):
            body_text = await resp.text()
            raise RuntimeError(
                f"Qdrant upsert returned HTTP {resp.status}: {body_text[:200]}"
            )


async def _require_collection_alias(session: aiohttp.ClientSession) -> None:
    """Fail closed unless the configured Qdrant alias already exists."""
    async with session.get(
        f"{QDRANT_URL}/aliases",
        timeout=aiohttp.ClientTimeout(total=QDRANT_TIMEOUT_S),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Qdrant alias lookup returned HTTP {resp.status}.")
        aliases = (await resp.json()).get("result", {}).get("aliases", [])
    if not any(alias.get("alias_name") == COLLECTION for alias in aliases):
        raise RuntimeError(
            f"Required Qdrant alias {COLLECTION!r} is absent; run the migration/bootstrap job."
        )


# ---------------------------------------------------------------------------
# deep-web-mcp extraction
# ---------------------------------------------------------------------------

async def _extract_via_mcp(
    session:   aiohttp.ClientSession,
    url:       str,
    thread_id: str,
) -> str:
    """
    Call deep-web-mcp's fetch_deep_web_data tool via the REST bridge and
    return the sanitized Markdown payload.

    Uses POST /extract/stream (SSE) to receive the full extraction result.
    SSE frames are consumed until a 'result' or 'error' event is received.

    Raises
    ------
    EgressTimeoutBreachError
        When deep-web-mcp returns error_code == "EGRESS_TIMEOUT_BREACH".
    RuntimeError
        For any other non-200 or parsing failure.
    """
    payload = {
        "url":              url,
        "thread_id":        thread_id,
        "session_required": False,
    }

    logger.info("[MONITOR] Invoking deep-web-mcp extraction — url=%r", url)

    # Use the synchronous POST endpoint for simplicity (avoids SSE parsing overhead)
    # The daemon is not latency-critical — blocking on the extraction result is acceptable.
    mcp_tool_url = f"{MCP_URL}/extract/stream"

    async with session.post(
        mcp_tool_url,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=MCP_TIMEOUT_S),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(
                f"deep-web-mcp returned HTTP {resp.status} for url={url!r}"
            )

        # Parse the SSE stream: read lines until we see 'event: result' or 'event: error'
        content = await resp.text()

    # SSE response is newline-delimited; parse each event block
    current_event = None
    content_str   = ""

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            if current_event == "result":
                try:
                    data = json.loads(data_str)
                    return data.get("content", "")
                except json.JSONDecodeError:
                    raise RuntimeError(f"Malformed result event data: {data_str[:200]}")
            elif current_event == "error":
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    data = {}
                error_code = data.get("error_code", "UNKNOWN")
                reason     = data.get("reason", "Unknown extraction error.")
                if error_code == "EGRESS_TIMEOUT_BREACH":
                    raise EgressTimeoutBreachError(
                        f"EGRESS_TIMEOUT_BREACH from deep-web-mcp: {reason}"
                    )
                raise RuntimeError(
                    f"deep-web-mcp error [{error_code}]: {reason}"
                )

    raise RuntimeError(
        "deep-web-mcp SSE stream ended without a 'result' or 'error' event."
    )


class EgressTimeoutBreachError(RuntimeError):
    """Raised when deep-web-mcp returns EGRESS_TIMEOUT_BREACH."""


# ---------------------------------------------------------------------------
# LangGraph alert dispatch
# ---------------------------------------------------------------------------

async def _dispatch_alert(
    session:      aiohttp.ClientSession,
    url:          str,
    distance:     float,
    new_payload:  str,
    eval_id:      str,
) -> bool:
    """
    POST an asynchronous drift alert to the LangGraph orchestrator webhook.

    The orchestrator /webhook/alert endpoint is expected to accept:
      {
        "event":      "drift_detected",
        "eval_id":    str,
        "url":        str,
        "distance":   float,
        "threshold":  float,
        "content":    str,        ← sanitized DOM snapshot
        "timestamp":  float,
      }

    This call is fire-and-forget from the evaluation cycle's perspective —
    failure is logged but does not affect the Qdrant update or cycle outcome.
    """
    alert_payload = {
        "event":     "drift_detected",
        "eval_id":   eval_id,
        "url":       url,
        "distance":  round(distance, 6),
        "threshold": DRIFT_THRESHOLD,
        "content":   new_payload[:4096],   # trim to avoid HTTP payload overflow
        "timestamp": time.time(),
    }

    try:
        async with session.post(
            ALERT_ENDPOINT,
            json=alert_payload,
            timeout=aiohttp.ClientTimeout(total=ALERT_TIMEOUT_S),
        ) as resp:
            if resp.status in (200, 201, 202, 204):
                logger.info(
                    "[MONITOR] Alert dispatched successfully — eval_id=%s  status=%d",
                    eval_id, resp.status,
                )
                return True
            else:
                body = await resp.text()
                logger.warning(
                    "[MONITOR] Alert endpoint returned HTTP %d (non-fatal): %s",
                    resp.status, body[:200],
                )
    except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as exc:
        # Non-fatal: the orchestrator may be temporarily unavailable
        logger.warning(
            "[MONITOR] Alert dispatch failed (non-fatal): %s: %s",
            type(exc).__name__, exc,
        )
    return False


# ===========================================================================
# FastAPI application
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan: pre-warm the embedding model on startup
    so the first evaluation request does not bear the cold-load penalty.
    """
    logger.info("[MONITOR] Daemon starting — pre-warming CPU embedding model...")
    try:
        await _get_embed_model()
        logger.info("[MONITOR] Embedding model ready.")
    except Exception as exc:
        logger.error("[MONITOR] Embedding model pre-warm failed: %s", exc)
    yield
    logger.info("[MONITOR] Daemon shutting down.")


app = FastAPI(
    title="Monitor Daemon",
    description=(
        "CPU-bound web change detection and semantic vector drift monitor. "
        "Integrates with deep-web-mcp for extraction and Qdrant for vector storage."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

_DASHBOARD_PATH = Path(__file__).with_name("monitor_dashboard.html")


async def _probe_backend(
    session: aiohttp.ClientSession,
    name: str,
    label: str,
    url: str,
    expected_statuses: tuple[int, ...] = (200,),
) -> dict:
    started = time.perf_counter()
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=3.0),
        ) as response:
            latency_ms = round((time.perf_counter() - started) * 1000)
            status = "reachable" if response.status in expected_statuses else "degraded"
            return {
                "name": name,
                "label": label,
                "status": status,
                "detail": (
                    f"Responding (HTTP {response.status})"
                    if status == "reachable"
                    else f"Unexpected HTTP {response.status}"
                ),
                "latency_ms": latency_ms,
            }
    except Exception as exc:
        return {
            "name": name,
            "label": label,
            "status": "offline",
            "detail": f"{type(exc).__name__}: {str(exc)[:120]}",
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }


async def _backend_connectivity() -> list[dict]:
    connector = aiohttp.TCPConnector(limit=3, ttl_dns_cache=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        return await asyncio.gather(
            _probe_backend(
                session,
                "deep_web_mcp",
                "Deep Web MCP",
                f"{MCP_URL}/extract/status/dashboard-connectivity-probe",
            ),
            _probe_backend(session, "qdrant", "Qdrant", f"{QDRANT_URL}/aliases"),
            _probe_backend(session, "orchestrator", "LangGraph Orchestrator", f"{ORCHESTRATOR_URL}/health"),
        )


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class EvaluateRequest(BaseModel):
    """
    Input contract for /monitor/evaluate.

    Fields
    ------
    url : str
        Allowed internal target URL to monitor.
    thread_id : str
        Session thread ID forwarded to deep-web-mcp for credential lookup.
    force_baseline : bool
        If True, always treat as a new baseline (overwrite existing vector).
    """
    url:            str            = Field(..., description="Target URL to monitor.")
    thread_id:      str            = Field("monitor-daemon", description="Session thread ID.")
    force_baseline: bool           = Field(False, description="Force baseline re-initialization.")


class EvaluateResponse(BaseModel):
    eval_id:    str
    url:        str
    outcome:    str            # baseline | unchanged | drift_detected | error
    distance:   Optional[float] = None
    threshold:  float          = DRIFT_THRESHOLD
    alert_sent: bool           = False
    message:    str            = ""
    timestamp:  float          = Field(default_factory=time.time)


# ===========================================================================
# ENDPOINT: POST /monitor/evaluate
# ===========================================================================

@app.post("/monitor/evaluate", response_model=EvaluateResponse)
async def evaluate(req: EvaluateRequest) -> EvaluateResponse:
    """
    Execute a full web change detection and semantic drift evaluation cycle.

    Pipeline
    --------
    1. Extract sanitized Markdown from target URL via deep-web-mcp.
    2. Generate CPU embedding (all-MiniLM-L6-v2, device=cpu, no VRAM).
    3. Query the configured Qdrant alias for the previously stored vector.
    4. Compute cosine distance.
    5a. 404 (first seen) → baseline init, store vector, exit without alert.
    5b. distance < DRIFT_THRESHOLD → log unchanged, exit.
    5c. distance ≥ DRIFT_THRESHOLD → update Qdrant, POST /webhook/alert.

    Error contracts
    ---------------
    - EGRESS_TIMEOUT_BREACH → log WARNING, return outcome="error", exit silently.
    - Any other error → log ERROR, return outcome="error".
    """
    eval_id   = str(uuid.uuid4())
    point_id  = _url_point_id(req.url)
    _start_operation(eval_id, req.url)
    _set_operation_step(eval_id, "extraction", "Fetching sanitized content from Deep Web MCP")

    logger.info(
        "[MONITOR] Evaluation started — eval_id=%s  url=%r",
        eval_id, req.url,
    )

    connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:

        # ── Step 1: deep-web-mcp extraction ───────────────────────────────
        try:
            content = await _extract_via_mcp(
                session=session,
                url=req.url,
                thread_id=req.thread_id,
            )
        except EgressTimeoutBreachError as exc:
            # Terminate cycle silently — no retry, no alert
            logger.warning(
                "[MONITOR] EGRESS_TIMEOUT_BREACH for url=%r — "
                "cycle terminated silently. reason=%s",
                req.url, exc,
            )
            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="error", error_code="EGRESS_TIMEOUT_BREACH",
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id, url=req.url,
                outcome="error",
                message=f"EGRESS_TIMEOUT_BREACH: {exc}",
            )
        except Exception as exc:
            logger.error(
                "[MONITOR] Extraction failed for url=%r: %s", req.url, exc
            )
            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="error", error_code="EXTRACTION_FAILURE",
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id, url=req.url,
                outcome="error",
                message=f"Extraction failure: {exc}",
            )

        if not content or not content.strip():
            logger.warning(
                "[MONITOR] Empty content returned for url=%r — skipping embedding.",
                req.url,
            )
            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="error", error_code="EMPTY_CONTENT",
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id, url=req.url,
                outcome="error",
                message="deep-web-mcp returned empty content.",
            )

        logger.info(
            "[MONITOR] Content received — len=%d chars  eval_id=%s",
            len(content), eval_id,
        )

        # ── Step 2: CPU embedding ──────────────────────────────────────────
        _set_operation_step(eval_id, "embedding", f"Embedding {len(content)} characters on CPU")
        try:
            new_vector = await _embed_text(content)
        except Exception as exc:
            logger.error("[MONITOR] Embedding failed for url=%r: %s", req.url, exc)
            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="error", error_code="EMBED_FAILURE",
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id, url=req.url,
                outcome="error",
                message=f"CPU embedding failure: {exc}",
            )

        embed_dim = len(new_vector)
        logger.debug("[MONITOR] Vector generated — dim=%d  eval_id=%s", embed_dim, eval_id)

        # Require an operator-created alias; request-time collection creation is forbidden.
        _set_operation_step(eval_id, "alias_check", f"Verifying Qdrant alias {COLLECTION}")
        try:
            await _require_collection_alias(session)
        except Exception as exc:
            logger.error("[MONITOR] Qdrant alias requirement failed: %s", exc)
            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="error", error_code="QDRANT_ALIAS_FAILURE",
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id,
                url=req.url,
                outcome="error",
                message=str(exc),
            )

        # ── Step 3: Qdrant lookup ──────────────────────────────────────────
        _set_operation_step(eval_id, "vector_lookup", "Looking up the previous observation")
        existing_point = None
        if not req.force_baseline:
            try:
                existing_point = await _qdrant_lookup(session, point_id)
            except Exception as exc:
                logger.warning(
                    "[MONITOR] Qdrant lookup failed (treating as baseline): %s", exc
                )

        # ── Baseline initialization path (404 / force_baseline) ───────────
        if existing_point is None:
            logger.info(
                "[MONITOR] [BASELINE ESTABLISHED] — url=%r  eval_id=%s  dim=%d",
                req.url, eval_id, embed_dim,
            )
            qdrant_payload = {
                "url":       req.url,
                "eval_id":   eval_id,
                "content":   content[:2048],   # store excerpt for provenance
                "timestamp": time.time(),
            }
            _set_operation_step(eval_id, "vector_write", "Persisting a new baseline vector")
            try:
                await _qdrant_upsert(session, point_id, new_vector, qdrant_payload)
                logger.info(
                    "[MONITOR] Baseline vector stored — point_id=%s", point_id
                )
            except Exception as exc:
                logger.error(
                    "[MONITOR] Failed to store baseline vector for url=%r: %s",
                    req.url, exc,
                )

            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="baseline",
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id, url=req.url,
                outcome="baseline",
                message="[BASELINE ESTABLISHED] — first observation stored. LLM not awakened.",
            )

        # ── Step 4: Extract stored vector and compute cosine distance ──────
        _set_operation_step(eval_id, "distance", "Calculating semantic cosine distance")
        try:
            stored_vector: list[float] = existing_point.get("vector", [])
            if not stored_vector:
                raise ValueError("Stored point has no vector data.")
            distance = _cosine_distance(new_vector, stored_vector)
        except Exception as exc:
            logger.error(
                "[MONITOR] Distance computation failed for url=%r: %s", req.url, exc
            )
            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="error", error_code="DISTANCE_COMPUTATION_FAILED",
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id, url=req.url,
                outcome="error",
                message=f"Distance computation failed: {exc}",
            )

        logger.info(
            "[MONITOR] Cosine distance=%.6f  threshold=%.4f  url=%r  eval_id=%s",
            distance, DRIFT_THRESHOLD, req.url, eval_id,
        )

        # ── Step 5a: Below threshold — no action ───────────────────────────
        if distance < DRIFT_THRESHOLD:
            logger.info(
                "[MONITOR] No significant drift detected — url=%r  "
                "distance=%.6f < threshold=%.4f",
                req.url, distance, DRIFT_THRESHOLD,
            )
            record = EvalRecord(
                eval_id=eval_id, url=req.url,
                timestamp=time.time(),
                outcome="unchanged", distance=distance,
            )
            _record_eval(record)
            return EvaluateResponse(
                eval_id=eval_id, url=req.url,
                outcome="unchanged",
                distance=distance,
                message=(
                    f"No semantic drift detected. "
                    f"distance={distance:.6f} < threshold={DRIFT_THRESHOLD}"
                ),
            )

        # ── Step 5b: Drift detected — update Qdrant + send alert ───────────
        logger.warning(
            "[MONITOR] ⚠ DRIFT DETECTED — url=%r  "
            "distance=%.6f ≥ threshold=%.4f  eval_id=%s",
            req.url, distance, DRIFT_THRESHOLD, eval_id,
        )

        # Update Qdrant with the new vector and content snapshot
        qdrant_payload = {
            "url":          req.url,
            "eval_id":      eval_id,
            "content":      content[:2048],
            "timestamp":    time.time(),
            "prev_distance": distance,
        }
        _set_operation_step(eval_id, "vector_write", "Persisting the changed content vector")
        try:
            await _qdrant_upsert(session, point_id, new_vector, qdrant_payload)
            logger.info(
                "[MONITOR] Qdrant record updated with new vector — point_id=%s",
                point_id,
            )
        except Exception as exc:
            logger.error(
                "[MONITOR] Qdrant update failed (non-fatal): %s", exc
            )

        # Dispatch alert to LangGraph orchestrator (fire-and-forget)
        _set_operation_step(eval_id, "alert", "Dispatching drift alert to LangGraph")
        alert_sent = False
        try:
            alert_sent = await _dispatch_alert(
                session=session,
                url=req.url,
                distance=distance,
                new_payload=content,
                eval_id=eval_id,
            )
        except Exception as exc:
            logger.warning("[MONITOR] Alert dispatch raised: %s", exc)

        record = EvalRecord(
            eval_id=eval_id, url=req.url,
            timestamp=time.time(),
            outcome="drift_detected", distance=distance,
        )
        _record_eval(record)

        return EvaluateResponse(
            eval_id=eval_id, url=req.url,
            outcome="drift_detected",
            distance=distance,
            alert_sent=alert_sent,
            message=(
                f"Semantic drift detected and recorded. "
                f"distance={distance:.6f} ≥ threshold={DRIFT_THRESHOLD}. "
                f"LangGraph alert {'dispatched' if alert_sent else 'dispatch failed (non-fatal)'}."
            ),
        )


# ===========================================================================
# ENDPOINTS: Dashboard and consolidated operations overview
# ===========================================================================

@app.get("/", include_in_schema=False)
async def dashboard():
    """Serve the local operator dashboard."""
    if not _DASHBOARD_PATH.exists():
        raise HTTPException(status_code=503, detail="Dashboard asset is unavailable.")
    return FileResponse(_DASHBOARD_PATH, media_type="text/html")


@app.get("/monitor/overview")
async def operations_overview():
    """Return the live dashboard data contract in one request."""
    backends = await _backend_connectivity()
    history = list(reversed(_eval_history))[:20]
    error_count = sum(record.outcome == "error" for record in _eval_history)
    offline_count = sum(backend["status"] == "offline" for backend in backends)
    degraded_count = sum(backend["status"] == "degraded" for backend in backends)
    latest_failed = bool(history and history[0].outcome == "error")
    if offline_count or latest_failed:
        system_state = "Attention needed"
    elif degraded_count or not (_embed_model is not None):
        system_state = "Degraded"
    else:
        system_state = "Operational"

    return {
        "summary": {
            "system_state": system_state,
            "active_operations": len(_active_operations),
            "total_evaluations": len(_eval_history),
            "error_rate_percent": (
                (error_count / len(_eval_history)) * 100 if _eval_history else 0.0
            ),
            "uptime_seconds": round(time.time() - _started_at),
            "model_loaded": _embed_model is not None,
        },
        "backends": backends,
        "active_operations": [asdict(operation) for operation in _active_operations.values()],
        "recent_operations": [asdict(operation) for operation in _recent_operations],
        "history": [asdict(record) for record in history],
        "timestamp": time.time(),
    }


# ===========================================================================
# ENDPOINT: GET /monitor/status
# ===========================================================================

@app.get("/monitor/status")
async def daemon_status():
    """
    Health check endpoint.  Returns embedding model load state,
    configuration summary, and evaluation count.
    """
    model_loaded = _embed_model is not None
    dim = None
    if model_loaded:
        try:
            dim = _embed_model.get_sentence_embedding_dimension()
        except Exception:
            pass

    return {
        "status":        "ok",
        "model_loaded":  model_loaded,
        "model_name":    EMBED_MODEL_NAME,
        "embed_dim":     dim,
        "device":        "cpu",
        "cpu_threads":   CPU_THREADS,
        "qdrant_url":    QDRANT_URL,
        "collection":    COLLECTION,
        "drift_threshold": DRIFT_THRESHOLD,
        "mcp_url":       MCP_URL,
        "alert_endpoint": ALERT_ENDPOINT,
        "eval_count":    len(_eval_history),
        "active_operations": len(_active_operations),
        "uptime_seconds": round(time.time() - _started_at),
        "timestamp":     time.time(),
    }


# ===========================================================================
# ENDPOINT: GET /monitor/history
# ===========================================================================

@app.get("/monitor/history")
async def evaluation_history(limit: int = 20):
    """
    Return the most recent evaluation records (newest first).
    Max limit: 100.
    """
    limit   = min(limit, _HISTORY_MAX)
    records = list(reversed(_eval_history))[:limit]
    return {
        "count":   len(records),
        "records": [
            {
                "eval_id":    r.eval_id,
                "url":        r.url,
                "outcome":    r.outcome,
                "distance":   r.distance,
                "error_code": r.error_code,
                "timestamp":  r.timestamp,
            }
            for r in records
        ],
    }


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000, reload=False)
