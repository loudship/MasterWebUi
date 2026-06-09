"""
qdrant_segregated_memory.py — Open-WebUI Inlet Filter Pipeline
===============================================================

Intercepts every conversational request and performs agent-persona-aware
vector memory injection before the message reaches the inference engine.

Routing matrix
--------------
  Active persona       │ Collection                │ Access mode
  ─────────────────────┼───────────────────────────┼─────────────────────
  Continuity_Checker   │ canon_master_space         │ Read-only (strict)
  World_Simulator      │ simulation_volatile_space  │ Read + flush tool
  (any other / none)   │ canon_master_space         │ Read-only (default)

Core behaviours
---------------
1. Reads the FIRST system message in the message array to detect the active
   agent persona signature (looks for "Continuity_Checker" or "World_Simulator"
   as a substring of the system content).

2. Embeds the user message text through the canonical inference gateway and
   queries the appropriate Qdrant alias.

3. Injects retrieved context blocks as an IMMUTABLE, ANCHORED block prepended
   to the very first position of the system message content — preserving LM
   Studio KV-cache alignment.  The injected block is wrapped in clear sentinel
   markers so downstream nodes can identify it without regex fragility.

4. Refuses runtime collection reset operations; aliases are maintenance-owned.

5. On ANY Qdrant error, logs locally and passes the body through unmodified.

Valve parameters (configurable in Open-WebUI admin UI)
------------------------------------------------------
  QDRANT_URL            : Qdrant REST base URL
  EMBEDDING_URL         : Canonical gateway embeddings endpoint
  SIMILARITY_THRESHOLD  : Cosine similarity floor (0.0–1.0)
  MAX_CONTEXT_RESULTS   : Max vector hits to inject per turn
  QDRANT_TIMEOUT_S      : Seconds before Qdrant calls are abandoned
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, List, Optional, Union, Generator, Iterator

import aiohttp
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("qdrant_segregated_memory")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ---------------------------------------------------------------------------
# Collection identifiers (immutable — never overridden via valves)
# ---------------------------------------------------------------------------

COLLECTION_CANON: str = os.environ.get("QDRANT_CANON_ALIAS", "narrative_active")
COLLECTION_VOLATILE: str = os.environ.get("QDRANT_SIMULATION_ALIAS", "simulation_active")

# Persona signature strings (substring match against system message content)
PERSONA_CONTINUITY_CHECKER: str = "Continuity_Checker"
PERSONA_WORLD_SIMULATOR:    str = "World_Simulator"

# Injected context block sentinels (anchored at top of system prompt)
_CTX_OPEN:  str = "<!-- [MEMORY_CONTEXT_BLOCK:BEGIN] -->"
_CTX_CLOSE: str = "<!-- [MEMORY_CONTEXT_BLOCK:END] -->"

# Vector dimensionality for the local embedding model
# LM Studio default all-minilm-l6-v2 → 384 dims.
# Override if a different model is loaded.
_EMBED_DIM_DEFAULT: int = 384


# ===========================================================================
# Pipeline
# ===========================================================================

class Pipeline:
    """
    Open-WebUI Filter Pipeline: Qdrant Segregated Memory Router

    Lifecycle hooks
    ---------------
    on_startup()  — verify Qdrant connectivity; ensure both collections exist.
    on_shutdown() — close shared aiohttp session.
    inlet()       — persona detection → vector retrieval → context injection.
    outlet()      — pass-through (no post-processing required).
    """

    # -----------------------------------------------------------------------
    # Valves
    # -----------------------------------------------------------------------

    class Valves(BaseModel):
        # Qdrant REST endpoint (local, no WAN)
        QDRANT_URL: str = Field(
            default=os.environ.get("QDRANT_URI", "http://localhost:6333"),
            description="Qdrant REST base URL (local port 6333).",
        )
        # Canonical gateway embeddings endpoint
        EMBEDDING_URL: str = Field(
            default=os.environ.get(
                "INFERENCE_GATEWAY_URL",
                "http://inference-gateway:4321",
            ) + "/v1/embeddings",
            description="Canonical gateway /v1/embeddings endpoint.",
        )
        # Cosine similarity floor — hits below this score are discarded
        SIMILARITY_THRESHOLD: float = Field(
            default=0.75,
            description="Cosine similarity threshold for context inclusion (0.0–1.0).",
        )
        # Maximum number of context chunks to inject per turn
        MAX_CONTEXT_RESULTS: int = Field(
            default=5,
            description="Maximum vector search hits injected per turn.",
        )
        # Seconds before Qdrant/embedding calls are abandoned
        QDRANT_TIMEOUT_S: float = Field(
            default=8.0,
            description="Timeout in seconds for Qdrant and embedding HTTP calls.",
        )
        # Vector dimension for the active embedding model
        EMBED_DIM: int = Field(
            default=_EMBED_DIM_DEFAULT,
            description="Embedding vector dimensions (must match Qdrant collection config).",
        )

    # -----------------------------------------------------------------------
    # Constructor
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        self.name    = "Qdrant Segregated Memory Router"
        self.valves  = self.Valves()
        self._session: Optional[aiohttp.ClientSession] = None

    # -----------------------------------------------------------------------
    # Lifecycle hooks
    # -----------------------------------------------------------------------

    async def on_startup(self) -> None:
        """
        Open a persistent aiohttp session and ensure both Qdrant collections
        exist with Cosine distance.  Failures are logged but do not abort startup.
        """
        connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=connector)
        logger.info("[MEMORY ROUTER] Pipeline started — verifying Qdrant collections.")

        for collection_name in (COLLECTION_CANON, COLLECTION_VOLATILE):
            try:
                await self._ensure_collection(collection_name)
            except Exception as exc:
                logger.warning(
                    "[MEMORY ROUTER] Could not verify collection %r on startup: %s",
                    collection_name, exc,
                )

    async def on_shutdown(self) -> None:
        """Close the shared aiohttp session gracefully."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("[MEMORY ROUTER] aiohttp session closed.")

    # -----------------------------------------------------------------------
    # inlet  — the primary filter hook
    # -----------------------------------------------------------------------

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        """
        Intercept the request body before it reaches the inference engine.

        Steps
        -----
        1. Detect active agent persona from the first system message.
        2. Select target collection + access mode.
        3. Embed the latest user message.
        4. Query Qdrant for semantically similar context chunks.
        5. Filter hits by SIMILARITY_THRESHOLD.
        6. Prepend an immutable, anchored context block to the system message.

        On any failure: log warning → pass body through unmodified.
        """
        messages: list = body.get("messages", [])
        if not messages:
            return body

        # ── Step 1: Detect persona ─────────────────────────────────────────
        persona    = self._detect_persona(messages)
        collection = self._resolve_collection(persona)
        read_only  = (persona != PERSONA_WORLD_SIMULATOR)

        logger.info(
            "[MEMORY ROUTER] inlet — persona=%r  collection=%r  read_only=%s",
            persona, collection, read_only,
        )

        # ── Step 2: Extract the user message for embedding ─────────────────
        user_text = self._extract_user_text(messages)
        if not user_text:
            logger.debug("[MEMORY ROUTER] No user message text — skipping memory injection.")
            return body

        # ── Steps 3–5: Embed + query + filter ─────────────────────────────
        try:
            context_chunks = await self._retrieve_context(
                query_text=user_text,
                collection=collection,
            )
        except Exception as exc:
            logger.warning(
                "[MEMORY ROUTER] Vector retrieval failed for persona=%r alias=%r: %s",
                persona,
                collection,
                exc,
            )
            return body   # graceful pass-through — never drop the chain

        if not context_chunks:
            logger.debug(
                "[MEMORY ROUTER] No context chunks above threshold %.2f — "
                "proceeding without injection.",
                self.valves.SIMILARITY_THRESHOLD,
            )
            return body

        # ── Step 6: Inject anchored context block at top of system prompt ──
        body = self._inject_context_block(body, context_chunks, persona)
        logger.info(
            "[MEMORY ROUTER] Injected %d context chunk(s) from %r into system prompt.",
            len(context_chunks), collection,
        )
        return body

    # -----------------------------------------------------------------------
    # outlet  — pass-through
    # -----------------------------------------------------------------------

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        return body

    # -----------------------------------------------------------------------
    # Tool endpoint: flush_simulation_cache
    # Callable from Open-WebUI tool invocation when World_Simulator is active.
    # -----------------------------------------------------------------------

    async def flush_simulation_cache(self) -> str:
        """
        Refuse runtime collection lifecycle mutation.
        """
        logger.warning("[MEMORY ROUTER] Runtime collection reset was denied.")
        return json.dumps({
            "status": "error",
            "message": "Runtime collection reset is forbidden; use a staged alias cutover.",
        })

    # =======================================================================
    # Private helpers
    # =======================================================================

    # ── Persona detection ──────────────────────────────────────────────────

    def _detect_persona(self, messages: list) -> str:
        """
        Inspect the FIRST system message for active agent persona signature.

        Matching is case-sensitive substring search against the canonical
        persona identifiers defined in the orchestrator graph node names.

        Returns the matched persona string, or an empty string if no match.
        """
        for msg in messages:
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                # Some frontends pass content as a list of blocks
                content = " ".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            if PERSONA_CONTINUITY_CHECKER in content:
                logger.debug("[MEMORY ROUTER] Persona detected: Continuity_Checker")
                return PERSONA_CONTINUITY_CHECKER
            if PERSONA_WORLD_SIMULATOR in content:
                logger.debug("[MEMORY ROUTER] Persona detected: World_Simulator")
                return PERSONA_WORLD_SIMULATOR
            break  # only inspect the FIRST system message
        logger.debug("[MEMORY ROUTER] No persona signature found — defaulting to canon read-only.")
        return ""

    def _resolve_collection(self, persona: str) -> str:
        """Map persona → collection name."""
        if persona == PERSONA_WORLD_SIMULATOR:
            return COLLECTION_VOLATILE
        return COLLECTION_CANON   # Continuity_Checker and default both use canon

    # ── User text extraction ───────────────────────────────────────────────

    def _extract_user_text(self, messages: list) -> str:
        """
        Return the content of the most recent user-role message.
        Returns empty string if no user message exists.
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Multi-modal content block list
                    return " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    ).strip()
                return str(content).strip()
        return ""

    # ── Embedding ─────────────────────────────────────────────────────────

    async def _embed_text(self, text: str) -> list[float]:
        """
        Generate a dense vector for *text* via LM Studio /v1/embeddings.

        Uses the shared aiohttp session with a strict timeout so heavy
        GPU-bound token generation on the host does not starve the pipeline.

        Raises on any HTTP or connection error — callers must handle.
        """
        session = await self._get_session()
        payload = {
            "model": "text-embedding-nomic-embed-text-v1.5",  # LM Studio default
            "input": text[:8192],   # guard against oversized inputs
        }
        async with session.post(
            self.valves.EMBEDDING_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=self.valves.QDRANT_TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Embedding endpoint returned HTTP {resp.status}"
                )
            data = await resp.json(content_type=None)
            embedding = data["data"][0]["embedding"]
            if len(embedding) != self.valves.EMBED_DIM:
                logger.warning(
                    "[MEMORY ROUTER] Embedding dim mismatch: expected %d, got %d. "
                    "Update EMBED_DIM valve to match the active model.",
                    self.valves.EMBED_DIM, len(embedding),
                )
            return embedding

    # ── Qdrant retrieval ───────────────────────────────────────────────────

    async def _retrieve_context(
        self,
        query_text: str,
        collection: str,
    ) -> list[dict]:
        """
        Embed *query_text*, query *collection* with cosine similarity,
        and return a list of result dicts filtered to SIMILARITY_THRESHOLD.

        Each result dict:
            {"score": float, "text": str, "metadata": dict}

        Raises on Qdrant connectivity failures — callers must handle.
        """
        vector = await self._embed_text(query_text)

        session = await self._get_session()
        search_payload = {
            "vector":       vector,
            "limit":        self.valves.MAX_CONTEXT_RESULTS,
            "score_threshold": self.valves.SIMILARITY_THRESHOLD,
            "with_payload": True,
            "with_vector":  False,
        }

        async with session.post(
            f"{self.valves.QDRANT_URL}/collections/{collection}/points/search",
            json=search_payload,
            timeout=aiohttp.ClientTimeout(total=self.valves.QDRANT_TIMEOUT_S),
        ) as resp:
            if resp.status == 404:
                logger.warning(
                    "[MEMORY ROUTER] Collection %r not found in Qdrant — "
                    "returning empty context.",
                    collection,
                )
                return []
            if resp.status != 200:
                raise RuntimeError(
                    f"Qdrant search on {collection!r} returned HTTP {resp.status}"
                )
            data = await resp.json(content_type=None)

        results = data.get("result", [])
        chunks  = []
        for hit in results:
            score   = hit.get("score", 0.0)
            payload = hit.get("payload", {})
            text    = payload.get("text", payload.get("content", ""))
            if not text:
                continue
            chunks.append({
                "score":    score,
                "text":     text,
                "metadata": {k: v for k, v in payload.items() if k not in ("text", "content")},
            })

        logger.debug(
            "[MEMORY ROUTER] Qdrant %r: %d hit(s) above threshold %.2f.",
            collection, len(chunks), self.valves.SIMILARITY_THRESHOLD,
        )
        return chunks

    # ── Context injection ─────────────────────────────────────────────────

    def _inject_context_block(
        self,
        body:           dict,
        context_chunks: list[dict],
        persona:        str,
    ) -> dict:
        """
        Prepend an immutable, anchored context block to the FIRST system
        message's content string.

        Injection format
        ----------------
        <!-- [MEMORY_CONTEXT_BLOCK:BEGIN] -->
        [Retrieved Context — persona: <X> | collection: <Y> | hits: N]

        [1] (score=0.92) <chunk text>

        [2] (score=0.87) <chunk text>
        <!-- [MEMORY_CONTEXT_BLOCK:END] -->

        <original system content>

        The block is placed BEFORE the original system content so it occupies
        the absolute beginning of the KV-cache sequence, enabling LM Studio to
        reuse the cached context prefix across successive turns without
        re-encoding it.

        If no system message exists, a synthetic one is inserted at index 0.
        """
        messages: list = body.get("messages", [])
        collection     = self._resolve_collection(persona)

        # Build the context block text
        chunk_lines = []
        for idx, chunk in enumerate(context_chunks, start=1):
            score_str = f"score={chunk['score']:.4f}"
            chunk_lines.append(f"[{idx}] ({score_str}) {chunk['text'].strip()}")

        header = (
            f"[Retrieved Context — persona: {persona or 'default'} | "
            f"collection: {collection} | "
            f"hits: {len(context_chunks)} | "
            f"threshold: {self.valves.SIMILARITY_THRESHOLD}]"
        )
        context_block = (
            f"{_CTX_OPEN}\n"
            f"{header}\n\n"
            + "\n\n".join(chunk_lines)
            + f"\n{_CTX_CLOSE}"
        )

        # Find the first system message and prepend
        system_idx = None
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                system_idx = i
                break

        if system_idx is not None:
            original = messages[system_idx].get("content", "")
            if isinstance(original, list):
                # Multi-modal block list: prepend as a text block
                messages[system_idx]["content"] = [
                    {"type": "text", "text": context_block + "\n\n"}
                ] + original
            else:
                messages[system_idx]["content"] = (
                    context_block + "\n\n" + original
                )
        else:
            # No system message exists — insert a synthetic one at position 0
            messages.insert(0, {
                "role":    "system",
                "content": context_block,
            })

        body["messages"] = messages
        return body

    # ── Qdrant collection bootstrap ────────────────────────────────────────

    async def _ensure_collection(self, collection_name: str) -> None:
        """
        Verify that the configured alias resolves in Qdrant.
        """
        session = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=self.valves.QDRANT_TIMEOUT_S)

        async with session.get(
            f"{self.valves.QDRANT_URL}/collections/{collection_name}",
            timeout=timeout,
        ) as resp:
            if resp.status == 200:
                logger.info(
                    "[MEMORY ROUTER] Collection %r exists — OK.", collection_name
                )
                return
            if resp.status != 404:
                raise RuntimeError(
                    f"Unexpected status {resp.status} checking {collection_name!r}"
                )

        raise RuntimeError(
            f"Required Qdrant alias {collection_name!r} is absent; "
            "run the migration/bootstrap or atomic cutover utility."
        )

    # ── Session accessor ───────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """
        Return the shared aiohttp session, creating a new one if necessary.
        This handles cases where on_startup was not called (e.g. unit tests).
        """
        if self._session is None or self._session.closed:
            connector     = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session
