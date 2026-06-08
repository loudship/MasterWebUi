"""
langgraph_router.py — Open-WebUI Pipeline: Narrative Commit Orchestrator Gateway
==================================================================================

Routes user messages to the LangGraph narrative commit orchestrator.

Changes from v1
---------------
- Graceful handling of aiohttp connection timeouts / hardware saturation:
    ClientConnectorError, ServerDisconnectedError, asyncio.TimeoutError all
    return a user-readable message instead of propagating exceptions.
- Added /interrupt call path for retroactive prompt modification detection.
    When the user edits an upstream message, the pipeline cancels the active
    thread via POST /interrupt and re-issues the modified prompt.
- Tor circuit rotation logic is retained as-is (unchanged from v1) for
    resilience on 403 responses from the orchestrator.
- Added HITL_TIMEOUT_S valve (passed through to orchestrator metadata).
"""

import os
import asyncio
import aiohttp
import socket
from typing import List, Union, Generator, Iterator, Optional
from pydantic import BaseModel

# Persistence injection — retained from v1
os.environ["QDRANT_URI"] = "http://qdrant:6333"
os.environ["RAG_SYSTEM_CONTEXT"] = "True"

MAX_CHARS = 20_000

# ---------------------------------------------------------------------------
# Tor circuit-rotation constants (unchanged from v1)
# ---------------------------------------------------------------------------

TOR_CONTROL_HOST = os.environ.get("TOR_CONTROL_HOST", "tor-gateway")
TOR_CONTROL_PORT = int(os.environ.get("TOR_CONTROL_PORT", "9051"))
TOR_CONTROL_PASS = os.environ.get("TOR_CONTROL_PASSWORD", "")
TOR_SOCKS_HOST   = os.environ.get("TOR_SOCKS_HOST", "tor-gateway")
TOR_SOCKS_PORT   = int(os.environ.get("TOR_SOCKS_PORT", "9050"))
MAX_TOR_RETRIES  = 3
RETRY_BACKOFF    = [2, 4, 8]   # seconds per retry attempt


# ---------------------------------------------------------------------------
# Helper: signal Tor to rotate circuit via control port NEWNYM (unchanged)
# ---------------------------------------------------------------------------

async def _rotate_tor_circuit() -> bool:
    """
    Send NEWNYM signal to the Tor control port to force a new exit circuit.
    Returns True on success, False if control port is unreachable.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(TOR_CONTROL_HOST, TOR_CONTROL_PORT),
            timeout=4,
        )
        if TOR_CONTROL_PASS:
            writer.write(f'AUTHENTICATE "{TOR_CONTROL_PASS}"\r\n'.encode())
        else:
            writer.write(b"AUTHENTICATE\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=4)

        writer.write(b"SIGNAL NEWNYM\r\n")
        await writer.drain()
        nym_resp = await asyncio.wait_for(reader.readline(), timeout=4)

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        success = b"250" in nym_resp
        if success:
            print(f"[TOR ROTATION] New circuit requested — NEWNYM accepted.")
            await asyncio.sleep(1)
        else:
            print(f"[TOR ROTATION] NEWNYM response: {nym_resp.decode(errors='replace').strip()}")
        return success

    except (OSError, asyncio.TimeoutError, ConnectionRefusedError) as e:
        print(f"[TOR ROTATION] Control port unreachable ({TOR_CONTROL_HOST}:{TOR_CONTROL_PORT}): {e}")
        return False


# ---------------------------------------------------------------------------
# Helper: POST to LangGraph orchestrator with 403-retry + Tor rotation
# Graceful timeout handling added for hardware saturation edge case.
# ---------------------------------------------------------------------------

async def fetch_with_tor_retry(
    session: aiohttp.ClientSession,
    url:     str,
    payload: dict,
) -> tuple[int, dict]:
    """
    POST to *url* with *payload*.

    On HTTP 403 or connection error, rotate the Tor exit circuit and retry
    up to MAX_TOR_RETRIES times with exponential back-off.

    On aiohttp.ClientConnectorError or asyncio.TimeoutError (hardware saturation),
    returns (0, {"error": <human-readable message>}) without raising.

    Returns (status_code, response_dict).
    """
    last_status = 0
    last_data:  dict = {}

    for attempt in range(MAX_TOR_RETRIES + 1):
        try:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                last_status = resp.status
                if resp.status == 200:
                    last_data = await resp.json()
                    return last_status, last_data

                if resp.status == 403:
                    print(
                        f"[TOR RETRY] HTTP 403 on attempt {attempt + 1}/{MAX_TOR_RETRIES + 1}. "
                        f"Rotating Tor circuit..."
                    )
                    await _rotate_tor_circuit()
                else:
                    print(f"[TOR RETRY] Non-retryable HTTP {resp.status} — aborting retry loop.")
                    last_data = {"error": f"HTTP {resp.status}"}
                    return last_status, last_data

        except asyncio.TimeoutError:
            # ── Graceful timeout: hardware saturation ──────────────────────
            print(
                f"[PIPELINE] Request timed out on attempt {attempt + 1} "
                f"(orchestrator port may be experiencing hardware saturation). "
                f"Retrying after Tor rotation..."
            )
            last_status = 0
            last_data   = {
                "error": (
                    "The orchestration service timed out. This typically indicates "
                    "temporary hardware saturation on the background port. "
                    "Your request will be retried automatically."
                )
            }
            await _rotate_tor_circuit()

        except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, OSError) as exc:
            # ── Graceful connection error ──────────────────────────────────
            print(f"[PIPELINE] Connection error on attempt {attempt + 1}: {exc}. Rotating circuit...")
            last_status = 0
            last_data   = {
                "error": (
                    f"Unable to reach the orchestration service (attempt {attempt + 1}). "
                    f"The pipeline will retry after a Tor circuit rotation. "
                    f"If this persists, verify the orchestrator is running at the configured URL."
                )
            }
            await _rotate_tor_circuit()

        if attempt < MAX_TOR_RETRIES:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            print(f"[TOR RETRY] Waiting {wait}s before retry {attempt + 2}/{MAX_TOR_RETRIES + 1}...")
            await asyncio.sleep(wait)

    print(f"[TOR RETRY] All {MAX_TOR_RETRIES + 1} attempts exhausted. Returning last known status.")
    return last_status, last_data


# ===========================================================================
# Pipeline class
# ===========================================================================

class Pipeline:

    class Valves(BaseModel):
        LANGGRAPH_URL:   str   = "http://langgraph-orchestrator:8100/invoke"
        INTERRUPT_URL:   str   = "http://langgraph-orchestrator:8100/interrupt"
        HITL_TIMEOUT_S:  float = 120.0
        ENABLE_STREAMING: bool = False    # set True to use /stream SSE endpoint

    def __init__(self):
        self.valves = self.Valves()

        # Per-session state: maps session/user id → active thread_id
        # Used to detect upstream prompt modification and issue /interrupt calls.
        self._thread_registry: dict[str, str] = {}

    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    # -----------------------------------------------------------------------
    # inlet — truncate oversized messages
    # -----------------------------------------------------------------------

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        messages = body.get("messages", [])
        if messages:
            last = messages[-1]
            if last.get("role") == "user":
                content = last.get("content", "")
                if len(content) > MAX_CHARS:
                    last["content"] = content[:MAX_CHARS]
        return body

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

    # -----------------------------------------------------------------------
    # pipe — main dispatch
    # -----------------------------------------------------------------------

    async def pipe(
        self,
        user_message: str,
        model_id:     str,
        messages:     List[dict],
        body:         dict,
    ) -> Union[str, Generator, Iterator]:

        user_id   = (body.get("user", {}) or {}).get("id", "anonymous")
        thread_id = self._thread_registry.get(user_id)

        # ── Detect retroactive upstream prompt modification ─────────────────
        # If the user has an active thread and the last user message differs
        # from what we last sent, treat it as an upstream edit.
        last_committed = body.get("__last_committed_input__")
        is_retroactive_edit = (
            thread_id is not None
            and last_committed is not None
            and last_committed != user_message
        )

        if is_retroactive_edit:
            return await self._handle_interrupt(
                user_id=user_id,
                old_thread_id=thread_id,
                new_input=user_message,
            )

        # ── Normal invocation ───────────────────────────────────────────────
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "input":     user_message,
                    "thread_id": thread_id,   # None on first turn → server assigns new UUID
                }
                status, data = await fetch_with_tor_retry(
                    session, self.valves.LANGGRAPH_URL, payload,
                )

                if status == 200:
                    new_thread_id = data.get("thread_id")
                    if new_thread_id:
                        self._thread_registry[user_id] = new_thread_id

                    intent   = data.get("intent", "")
                    response = data.get("response", "")
                    prefix   = f"[{intent.upper()}] " if intent else ""
                    retry_ct = data.get("retry_count", 0)
                    suffix   = f"\n\n*(Resolved in {retry_ct} retry cycle(s))*" if retry_ct else ""
                    return (
                        f"{prefix}{response}{suffix}"
                        if response
                        else "Pipeline Error: Malformed response from LangGraph orchestrator."
                    )

                else:
                    error_detail = data.get("error", f"HTTP {status}")
                    return (
                        f"⚠️ **Pipeline Error**\n\n"
                        f"{error_detail}\n\n"
                        f"*(Failed after {MAX_TOR_RETRIES + 1} attempts with Tor circuit rotation)*"
                    )

        except Exception as exc:
            return (
                f"⚠️ **Pipeline Connection Error**\n\n"
                f"`{type(exc).__name__}: {exc}`\n\n"
                f"Verify the LangGraph orchestrator is reachable at: `{self.valves.LANGGRAPH_URL}`"
            )

    # -----------------------------------------------------------------------
    # _handle_interrupt — upstream prompt modification handler
    # -----------------------------------------------------------------------

    async def _handle_interrupt(
        self,
        user_id:      str,
        old_thread_id: str,
        new_input:    str,
    ) -> str:
        """
        Detect that the user has retroactively modified an upstream chat prompt.
        Issue POST /interrupt to the orchestrator to:
          1. Cancel the active graph task for old_thread_id.
          2. Clear stale thread state from the checkpointer.
          3. Rebuild the graph from the modified node index.
        """
        print(
            f"[PIPELINE] Upstream prompt modification detected for user={user_id!r}. "
            f"Interrupting thread_id={old_thread_id!r} and rebuilding state graph."
        )

        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "thread_id":  old_thread_id,
                    "new_input":  new_input,
                    "node_index": 0,    # restart from Semantic_Router_Node
                }
                async with session.post(
                    self.valves.INTERRUPT_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        new_thread_id = data.get("new_thread_id")
                        if new_thread_id:
                            self._thread_registry[user_id] = new_thread_id

                        intent   = data.get("intent", "")
                        response = data.get("response", "")
                        prefix   = f"[{intent.upper()}] " if intent else ""
                        return (
                            f"*(Prompt modified — state graph rebuilt from node 0)*\n\n"
                            f"{prefix}{response}"
                            if response
                            else "Pipeline Error: Malformed interrupt response from orchestrator."
                        )
                    else:
                        return (
                            f"⚠️ **Interrupt Error**: Orchestrator returned HTTP {resp.status}. "
                            f"The stale thread may still be running."
                        )

        except asyncio.TimeoutError:
            return (
                "⚠️ **Interrupt Timeout**: The orchestrator did not respond within the timeout window. "
                "This may indicate hardware saturation. Please retry your message."
            )
        except (aiohttp.ClientConnectorError, OSError) as exc:
            return (
                f"⚠️ **Interrupt Connection Error**: `{exc}`\n\n"
                f"Unable to reach interrupt endpoint: `{self.valves.INTERRUPT_URL}`"
            )
