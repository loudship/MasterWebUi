"""
hitl_broker.py — Async Redis-backed Human-in-the-Loop (HITL) Authorization Broker
====================================================================================

Architecture
------------
  Graph thread (high-risk tool call detected)
      │
      │  await HITLBroker.request_authorization(call_id, tool_name, tool_args)
      │
      ▼
  BLPOP  "hitl:auth:{call_id}"   ← blocks the coroutine (non-blocking to event loop)
      │
      │  Interface layer sends:
      │  LPUSH "hitl:auth:{call_id}"  <JSON {"approved": bool, "reason": str}>
      │
      ▼
  Returns AuthResult.APPROVED | AuthResult.DENIED | AuthResult.TIMEOUT

FastAPI endpoint
----------------
  POST /hitl/authorize
      Body: {"call_id": str, "approved": bool, "reason": str}
      → performs LPUSH to unblock the waiting coroutine

All Redis I/O is fully async via redis.asyncio (no WAN calls).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REDIS_URL: str = os.environ.get("REDIS_URL", "redis://redis-hitl:6379/0")
HITL_DEFAULT_TIMEOUT_S: float = float(os.environ.get("HITL_TIMEOUT_S", "120"))
HITL_KEY_PREFIX: str = "hitl:auth"


# ---------------------------------------------------------------------------
# Auth result enum
# ---------------------------------------------------------------------------

class AuthResult(str, Enum):
    APPROVED = "APPROVED"
    DENIED   = "DENIED"
    TIMEOUT  = "TIMEOUT"


# ---------------------------------------------------------------------------
# Pending call record
# ---------------------------------------------------------------------------

@dataclass
class PendingCall:
    call_id:   str
    tool_name: str
    tool_args: dict
    issued_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())


# ---------------------------------------------------------------------------
# HITLBroker
# ---------------------------------------------------------------------------

class HITLBroker:
    """
    Singleton-style broker.  Instantiate once at app startup and inject via
    LangGraph RunnableConfig or FastAPI dependency injection.

    Usage
    -----
    broker = HITLBroker()
    await broker.connect()

    result = await broker.request_authorization(
        call_id="abc123",
        tool_name="delete_database",
        tool_args={"db": "production"},
        timeout=120.0,
    )
    if result == AuthResult.APPROVED:
        ...
    """

    def __init__(self, redis_url: str = REDIS_URL) -> None:
        self._redis_url  = redis_url
        self._client: Optional[aioredis.Redis] = None
        self._pending: dict[str, PendingCall]  = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the Redis connection pool.  Call once at app startup."""
        try:
            self._client = aioredis.from_url(
                self._redis_url,
                decode_responses=False,   # raw bytes for token safety
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
            )
            logger.info("[HITL] Redis queue client configured: %s", self._redis_url)
        except Exception as exc:
            logger.error("[HITL] Redis connection failed: %s", exc)
            self._client = None

    async def disconnect(self) -> None:
        """Close the Redis connection pool.  Call at app shutdown."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("[HITL] Redis connection closed.")

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Core gate: BLPOP (blocks coroutine, not event loop)
    # ------------------------------------------------------------------

    async def request_authorization(
        self,
        tool_name: str,
        tool_args: dict,
        call_id:   Optional[str] = None,
        timeout:   float = HITL_DEFAULT_TIMEOUT_S,
    ) -> tuple[AuthResult, str]:
        """
        Block the calling coroutine until the interface layer responds via LPUSH,
        or until *timeout* seconds elapse.

        Parameters
        ----------
        tool_name : str   Name of the high-risk tool requesting authorization.
        tool_args : dict  Arguments that will be passed to the tool.
        call_id   : str   Unique identifier for this call (auto-generated if None).
        timeout   : float Seconds to wait before returning AuthResult.TIMEOUT.

        Returns
        -------
        (AuthResult, reason_str)
        """
        if not self._client:
            logger.warning("[HITL] Redis unavailable — defaulting to DENIED for safety.")
            return AuthResult.DENIED, "Redis unavailable; tool call blocked for safety."

        if call_id is None:
            call_id = str(uuid.uuid4())

        redis_key = f"{HITL_KEY_PREFIX}:{call_id}"

        # Pending call inspection is process-local. Redis is queue-only.
        pending = PendingCall(call_id=call_id, tool_name=tool_name, tool_args=tool_args)
        self._pending[call_id] = pending

        logger.info(
            "[HITL] Awaiting authorization — call_id=%s  tool=%s  timeout=%.0fs",
            call_id, tool_name, timeout,
        )

        try:
            # BLPOP with timeout: returns (key, value) tuple or None on timeout
            result = await self._client.blpop(redis_key, timeout=int(timeout))

            if result is None:
                logger.warning("[HITL] Timeout waiting for authorization: call_id=%s", call_id)
                return AuthResult.TIMEOUT, f"Authorization timed out after {timeout:.0f}s."

            _, raw_token = result
            return self._parse_token(raw_token, call_id)

        except (aioredis.RedisError, OSError) as exc:
            logger.error("[HITL] Redis error during BLPOP (call_id=%s): %s", call_id, exc)
            return AuthResult.DENIED, f"Redis error: {exc}"

        finally:
            self._pending.pop(call_id, None)

    # ------------------------------------------------------------------
    # Interface layer: LPUSH (called by FastAPI /hitl/authorize endpoint)
    # ------------------------------------------------------------------

    async def push_authorization(
        self,
        call_id:  str,
        approved: bool,
        reason:   str = "",
    ) -> bool:
        """
        Unblock a waiting request_authorization() call by pushing a token onto
        the Redis list key.

        Returns True if the key existed (call was pending), False otherwise.
        """
        if not self._client:
            logger.error("[HITL] Cannot push authorization — Redis unavailable.")
            return False
        if call_id not in self._pending:
            logger.warning("[HITL] Refusing authorization for non-pending call_id=%s.", call_id)
            return False

        redis_key = f"{HITL_KEY_PREFIX}:{call_id}"
        token_payload = json.dumps({
            "approved": approved,
            "reason":   reason,
            "call_id":  call_id,
        }).encode()

        pushed = await self._client.lpush(redis_key, token_payload)

        logger.info(
            "[HITL] Authorization pushed — call_id=%s  approved=%s  reason=%r",
            call_id, approved, reason,
        )
        return pushed > 0

    # ------------------------------------------------------------------
    # Pending call inspection
    # ------------------------------------------------------------------

    def get_pending_calls(self) -> list[dict]:
        """Return all currently pending HITL calls (for UI display)."""
        return [
            {
                "call_id":   p.call_id,
                "tool_name": p.tool_name,
                "tool_args": p.tool_args,
                "issued_at": p.issued_at,
            }
            for p in self._pending.values()
        ]

    def _parse_token(self, raw: bytes, call_id: str) -> tuple[AuthResult, str]:
        """Parse the LPUSH token from the interface layer."""
        try:
            payload = json.loads(raw.decode())
            approved = bool(payload.get("approved", False))
            reason   = payload.get("reason", "")
            result   = AuthResult.APPROVED if approved else AuthResult.DENIED
            logger.info("[HITL] Token parsed — call_id=%s  result=%s", call_id, result)
            return result, reason
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Fallback: treat raw bytes literally
            if raw in (b"APPROVED", b"1", b"true", b"True"):
                return AuthResult.APPROVED, "Raw token APPROVED"
            return AuthResult.DENIED, f"Unrecognised raw token: {raw!r}"


# ---------------------------------------------------------------------------
# FastAPI router — mount on the main app
# ---------------------------------------------------------------------------

hitl_router = APIRouter(prefix="/hitl", tags=["HITL"])

# Module-level broker instance; replaced at startup via broker_instance setter
_broker: Optional[HITLBroker] = None


def set_broker(broker: HITLBroker) -> None:
    global _broker
    _broker = broker


def get_broker() -> HITLBroker:
    if _broker is None:
        raise RuntimeError("HITLBroker has not been initialised. Call set_broker() at startup.")
    return _broker


# ------------------------------------------------------------------
# Pydantic request/response models
# ------------------------------------------------------------------

class AuthorizeRequest(BaseModel):
    call_id:  str
    approved: bool
    reason:   str = ""


class AuthorizeResponse(BaseModel):
    success:  bool
    call_id:  str
    approved: bool
    message:  str


class PendingCallsResponse(BaseModel):
    pending: list[dict]


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@hitl_router.post("/authorize", response_model=AuthorizeResponse)
async def hitl_authorize(req: AuthorizeRequest):
    """
    Push an authorization decision to a waiting graph coroutine.

    The graph thread is blocked on BLPOP for the matching call_id key.
    This endpoint performs LPUSH to unblock it.

    Example
    -------
    curl -X POST http://localhost:8100/hitl/authorize \\
         -H 'Content-Type: application/json' \\
         -d '{"call_id": "abc123", "approved": true, "reason": "Reviewed and safe"}'
    """
    broker = get_broker()
    success = await broker.push_authorization(
        call_id=req.call_id,
        approved=req.approved,
        reason=req.reason,
    )
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"No pending HITL call found for call_id={req.call_id!r}. "
                   "It may have already timed out or been resolved."
        )
    return AuthorizeResponse(
        success=True,
        call_id=req.call_id,
        approved=req.approved,
        message=f"Authorization {'granted' if req.approved else 'denied'} for call {req.call_id!r}.",
    )


@hitl_router.get("/pending", response_model=PendingCallsResponse)
async def hitl_pending():
    """List all currently pending HITL authorization requests."""
    broker = get_broker()
    return PendingCallsResponse(pending=broker.get_pending_calls())
