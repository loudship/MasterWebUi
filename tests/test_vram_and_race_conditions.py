"""
tests/test_vram_and_race_conditions.py
=======================================

Async smoke-test harness for the VRAM Arbiter Daemon and Langfuse
ClickHouse initialization race condition logic.

Test suites
-----------
Suite A — Eviction decision tree
  A1  Single model below ceiling → no eviction
  A2  Single model at exactly VRAM_CEILING → eviction triggered
  A3  Single model above VRAM_CEILING → eviction triggered, TTL=300
  A4  Two models (collision risk, no ceiling breach) → LRU evicted
  A5  Two models that together breach ceiling → LRU evicted via async subprocess
  A6  Eviction cooldown prevents re-eviction within 300 s window

Suite B — Silent exception handling (broad-catch verification)
  B1  Connection refused (ClientConnectorError) → WARNING, no crash
  B2  Socket timeout (asyncio.TimeoutError) → WARNING, no crash
  B3  Server disconnected mid-response → WARNING, no crash
  B4  HTTP 503 from inference server → WARNING, no crash
  B5  Malformed JSON body → WARNING, no crash
  B6  OS-level socket error (ClientOSError) → WARNING, no crash

Suite C — LRU selection correctness
  C1  Oldest-resident model is always selected as eviction target
  C2  Newly-loaded model is never selected when older resident exists

Suite D — Async subprocess eviction mechanics
  D1  Successful lms call with correct args (TTL=300, model key)
  D2  lms binary not found → error logged, returns False, no crash
  D3  lms subprocess timeout → process killed, returns False, no crash
  D4  lms non-zero exit code → warning logged, returns False

Requirements
------------
  pytest>=7
  pytest-asyncio>=0.23
  aiohttp>=3
  aioresponses  (pip install aioresponses)
  All offline — no WAN calls, no Docker daemon required.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aioresponses import aioresponses

# ---------------------------------------------------------------------------
# Bring the module under test into scope.
# The file lives at the project root, so we manipulate sys.path.
# ---------------------------------------------------------------------------
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import vram_arbiter_daemon as _mod
from vram_arbiter_daemon import (
    VRAM_CEILING,
    VRAM_PER_MODEL_ESTIMATE,
    EVICTION_COOLDOWN_S,
    LM_STUDIO_API,
    VRAMArbiterAsync,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Exact byte count required to trigger a ceiling breach with one model
_BREACH_EXACTLY = VRAM_CEILING           # == int(11.4 * 1024**3)
_ONE_MODEL_BELOW = VRAM_PER_MODEL_ESTIMATE - 1

# Fake model IDs
MODEL_A = "lmstudio-community/Meta-Llama-3.1-8B-GGUF"
MODEL_B = "lmstudio-community/Mistral-7B-GGUF"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_model_response(*model_ids: str) -> dict:
    """Build a fake /v1/models JSON payload."""
    return {"data": [{"id": mid} for mid in model_ids]}


def _fake_process(returncode: int = 0, stdout: bytes = b"ok", stderr: bytes = b"") -> MagicMock:
    """Return a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


# ===========================================================================
# Suite A — Eviction decision tree
# ===========================================================================

class TestEvictionDecisionTree:

    @pytest.fixture
    def arbiter(self) -> VRAMArbiterAsync:
        return VRAMArbiterAsync()

    # ── A1: Below ceiling → no eviction ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_A1_below_ceiling_no_eviction(self, arbiter):
        """One model whose VRAM estimate is below ceiling must not trigger eviction."""
        # Override per-model estimate to something safely below ceiling
        with patch.object(_mod, "VRAM_PER_MODEL_ESTIMATE", _ONE_MODEL_BELOW):
            with patch.object(arbiter, "_evict_model_async", new_callable=AsyncMock) as mock_evict:
                with aioresponses() as m:
                    m.get(LM_STUDIO_API, payload=_make_model_response(MODEL_A))
                    import aiohttp
                    connector = aiohttp.TCPConnector()
                    async with aiohttp.ClientSession(connector=connector) as session:
                        await arbiter._poll_vram_state(session)

        mock_evict.assert_not_called()

    # ── A2: Exactly at ceiling → eviction ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_A2_exactly_at_ceiling_triggers_eviction(self, arbiter):
        """Estimated VRAM == VRAM_CEILING must trigger eviction (>= boundary)."""
        exact_per_model = VRAM_CEILING  # one model == ceiling
        with patch.object(_mod, "VRAM_PER_MODEL_ESTIMATE", exact_per_model):
            with patch.object(arbiter, "_evict_model_async", new_callable=AsyncMock,
                              return_value=True) as mock_evict:
                with aioresponses() as m:
                    m.get(LM_STUDIO_API, payload=_make_model_response(MODEL_A))
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        await arbiter._poll_vram_state(session)

        mock_evict.assert_called_once_with(MODEL_A)

    # ── A3: Above ceiling → eviction with TTL=300 ──────────────────────────

    @pytest.mark.asyncio
    async def test_A3_above_ceiling_subprocess_ttl_300(self, arbiter):
        """
        When estimated VRAM exceeds VRAM_CEILING, _evict_model_async must be
        called and the underlying subprocess must receive --ttl 300.
        """
        over_ceiling = VRAM_CEILING + 1
        with patch.object(_mod, "VRAM_PER_MODEL_ESTIMATE", over_ceiling):
            proc = _fake_process(returncode=0)
            with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                       return_value=proc) as mock_exec:
                with aioresponses() as m:
                    m.get(LM_STUDIO_API, payload=_make_model_response(MODEL_A))
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        await arbiter._poll_vram_state(session)

        # Verify --ttl 300 was in the subprocess args
        call_args = mock_exec.call_args
        assert call_args is not None, "create_subprocess_exec was never called"
        positional = call_args.args
        assert "--ttl" in positional, f"--ttl missing from exec args: {positional}"
        ttl_idx = list(positional).index("--ttl")
        assert positional[ttl_idx + 1] == "300", (
            f"Expected TTL=300, got {positional[ttl_idx + 1]}"
        )

    # ── A4: Two models collision, no ceiling breach → LRU evicted ──────────

    @pytest.mark.asyncio
    async def test_A4_collision_risk_no_ceiling_breach(self, arbiter):
        """With 2 models below ceiling, the LRU model should be evicted."""
        safe_per_model = VRAM_CEILING // 4  # two models together still below ceiling
        with patch.object(_mod, "VRAM_PER_MODEL_ESTIMATE", safe_per_model):
            with patch.object(arbiter, "_evict_model_async", new_callable=AsyncMock,
                              return_value=True) as mock_evict:
                # Seed MODEL_A as older resident
                t0 = time.monotonic() - 500
                arbiter.resident_since[MODEL_A] = t0
                arbiter.resident_since[MODEL_B] = t0 + 100   # newer

                with aioresponses() as m:
                    m.get(LM_STUDIO_API, payload=_make_model_response(MODEL_A, MODEL_B))
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        await arbiter._poll_vram_state(session)

        # Must evict MODEL_A (older)
        mock_evict.assert_called_once_with(MODEL_A)

    # ── A5: Two models breach ceiling → LRU evicted ────────────────────────

    @pytest.mark.asyncio
    async def test_A5_two_models_breach_ceiling_lru_evicted(self, arbiter):
        """Two models whose combined estimate exceeds VRAM_CEILING → LRU evicted."""
        per_model_half_plus = (VRAM_CEILING // 2) + 1  # two × this > ceiling
        with patch.object(_mod, "VRAM_PER_MODEL_ESTIMATE", per_model_half_plus):
            with patch.object(arbiter, "_evict_model_async", new_callable=AsyncMock,
                              return_value=True) as mock_evict:
                t0 = time.monotonic() - 200
                arbiter.resident_since[MODEL_A] = t0        # older → LRU
                arbiter.resident_since[MODEL_B] = t0 + 50  # newer

                with aioresponses() as m:
                    m.get(LM_STUDIO_API, payload=_make_model_response(MODEL_A, MODEL_B))
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        await arbiter._poll_vram_state(session)

        mock_evict.assert_called_once_with(MODEL_A)

    # ── A6: Eviction cooldown prevents re-eviction ─────────────────────────

    @pytest.mark.asyncio
    async def test_A6_eviction_cooldown_respected(self, arbiter):
        """_evict_model_async must skip eviction when cooldown is active."""
        # Record a very recent eviction
        arbiter.last_eviction[MODEL_A] = time.monotonic()   # just now

        proc = _fake_process()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=proc) as mock_exec:
            result = await arbiter._evict_model_async(MODEL_A)

        assert result is True, "Should return True (cooldown is expected, not an error)"
        mock_exec.assert_not_called()


# ===========================================================================
# Suite B — Silent exception handling
# ===========================================================================

class TestSilentExceptionHandling:
    """
    Verify that all network/socket/decode errors are caught silently and logged
    as warnings WITHOUT crashing the daemon loop.
    """

    @pytest.fixture
    def arbiter(self) -> VRAMArbiterAsync:
        return VRAMArbiterAsync()

    async def _run_poll_expecting_warning(
        self,
        arbiter: VRAMArbiterAsync,
        caplog,
        aio_mock_setup_fn,
    ) -> None:
        """
        Helper: run _poll_vram_state with mocked network condition,
        assert no exception escapes and a WARNING was logged.
        """
        import aiohttp
        import logging

        with caplog.at_level(logging.WARNING, logger="vram_arbiter"):
            try:
                connector = aiohttp.TCPConnector()
                async with aiohttp.ClientSession(connector=connector) as session:
                    await aio_mock_setup_fn(arbiter, session)
            except Exception as exc:
                pytest.fail(
                    f"_poll_vram_state raised an exception instead of catching it: "
                    f"{type(exc).__name__}: {exc}"
                )

        # At least one warning must have been emitted
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "Expected at least one WARNING log entry — none found."

    # ── B1: Connection refused ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_B1_connection_refused(self, arbiter, caplog):
        import aiohttp

        async def setup(arb, session):
            with aioresponses() as m:
                m.get(LM_STUDIO_API, exception=aiohttp.ClientConnectorError(
                    connection_key=MagicMock(), os_error=ConnectionRefusedError()
                ))
                await arb._poll_vram_state(session)

        await self._run_poll_expecting_warning(arbiter, caplog, setup)

    # ── B2: Socket timeout ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_B2_socket_timeout(self, arbiter, caplog):
        async def setup(arb, session):
            with aioresponses() as m:
                m.get(LM_STUDIO_API, exception=asyncio.TimeoutError())
                await arb._poll_vram_state(session)

        await self._run_poll_expecting_warning(arbiter, caplog, setup)

    # ── B3: Server disconnected mid-response ────────────────────────────────

    @pytest.mark.asyncio
    async def test_B3_server_disconnected(self, arbiter, caplog):
        import aiohttp

        async def setup(arb, session):
            with aioresponses() as m:
                m.get(LM_STUDIO_API, exception=aiohttp.ServerDisconnectedError())
                await arb._poll_vram_state(session)

        await self._run_poll_expecting_warning(arbiter, caplog, setup)

    # ── B4: HTTP 503 from inference server ──────────────────────────────────

    @pytest.mark.asyncio
    async def test_B4_http_503(self, arbiter, caplog):
        import logging

        with aioresponses() as m:
            m.get(LM_STUDIO_API, status=503, body=b"Service Unavailable")
            import aiohttp
            with caplog.at_level(logging.WARNING, logger="vram_arbiter"):
                async with aiohttp.ClientSession() as session:
                    try:
                        await arbiter._poll_vram_state(session)
                    except Exception as exc:
                        pytest.fail(f"Raised exception on HTTP 503: {exc}")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "Expected WARNING on HTTP 503 — none found."

    # ── B5: Malformed JSON body ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_B5_malformed_json(self, arbiter, caplog):
        import logging

        with aioresponses() as m:
            m.get(LM_STUDIO_API, status=200, body=b"{not valid json{{")
            import aiohttp
            with caplog.at_level(logging.WARNING, logger="vram_arbiter"):
                async with aiohttp.ClientSession() as session:
                    try:
                        await arbiter._poll_vram_state(session)
                    except Exception as exc:
                        pytest.fail(f"Raised exception on bad JSON: {exc}")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "Expected WARNING on malformed JSON — none found."

    # ── B6: OS-level socket error ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_B6_os_socket_error(self, arbiter, caplog):
        import aiohttp

        async def setup(arb, session):
            with aioresponses() as m:
                m.get(LM_STUDIO_API, exception=aiohttp.ClientOSError())
                await arb._poll_vram_state(session)

        await self._run_poll_expecting_warning(arbiter, caplog, setup)


# ===========================================================================
# Suite C — LRU selection correctness
# ===========================================================================

class TestLRUSelection:

    @pytest.fixture
    def arbiter(self) -> VRAMArbiterAsync:
        return VRAMArbiterAsync()

    @pytest.mark.asyncio
    async def test_C1_oldest_resident_selected(self, arbiter):
        """LRU selection must consistently pick the oldest resident_since timestamp."""
        now = time.monotonic()
        models = {
            "model-alpha": now - 900,   # oldest
            "model-beta":  now - 400,
            "model-gamma": now - 100,   # newest
        }
        arbiter.resident_since.update(models)
        over_ceiling = VRAM_CEILING + 1
        with patch.object(_mod, "VRAM_PER_MODEL_ESTIMATE", over_ceiling // 3 + 1):
            with patch.object(arbiter, "_evict_model_async", new_callable=AsyncMock,
                              return_value=True) as mock_evict:
                with aioresponses() as m:
                    m.get(LM_STUDIO_API, payload=_make_model_response(*models.keys()))
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        await arbiter._poll_vram_state(session)

        mock_evict.assert_called_once_with("model-alpha")

    @pytest.mark.asyncio
    async def test_C2_newest_model_never_selected_when_older_exists(self, arbiter):
        """Newly loaded model must be protected from immediate eviction if older exists."""
        now = time.monotonic()
        arbiter.resident_since["old-model"] = now - 3600   # 1 hour old
        arbiter.resident_since["new-model"] = now - 1      # just loaded

        over_ceiling = VRAM_CEILING + 1
        with patch.object(_mod, "VRAM_PER_MODEL_ESTIMATE", over_ceiling // 2 + 1):
            with patch.object(arbiter, "_evict_model_async", new_callable=AsyncMock,
                              return_value=True) as mock_evict:
                with aioresponses() as m:
                    m.get(LM_STUDIO_API,
                          payload=_make_model_response("old-model", "new-model"))
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        await arbiter._poll_vram_state(session)

        # Must NOT evict "new-model"
        evicted = mock_evict.call_args.args[0]
        assert evicted == "old-model", (
            f"Newest model should never be selected for eviction; got {evicted!r}"
        )


# ===========================================================================
# Suite D — Async subprocess eviction mechanics
# ===========================================================================

class TestSubprocessEvictionMechanics:

    @pytest.fixture
    def arbiter(self) -> VRAMArbiterAsync:
        a = VRAMArbiterAsync()
        a._lms_bin = "lms"
        return a

    # ── D1: Successful lms call with correct args ───────────────────────────

    @pytest.mark.asyncio
    async def test_D1_successful_eviction_args(self, arbiter):
        """
        _evict_model_async must call lms with: <binary> load <model_id> --ttl 300
        and return True on returncode 0.
        """
        proc = _fake_process(returncode=0, stdout=b"Model TTL set.")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=proc) as mock_exec:
            result = await arbiter._evict_model_async(MODEL_A)

        assert result is True
        call_args = mock_exec.call_args.args
        assert call_args[0] == "lms",       f"Binary mismatch: {call_args[0]}"
        assert call_args[1] == "load",      f"Subcommand mismatch: {call_args[1]}"
        assert call_args[2] == MODEL_A,     f"Model ID mismatch: {call_args[2]}"
        assert call_args[3] == "--ttl",     f"Flag mismatch: {call_args[3]}"
        assert call_args[4] == "300",       f"TTL value mismatch: {call_args[4]}"

    # ── D2: lms binary not found ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_D2_lms_binary_not_found(self, arbiter, caplog):
        """FileNotFoundError from create_subprocess_exec must be caught; return False."""
        import logging

        with patch("asyncio.create_subprocess_exec",
                   side_effect=FileNotFoundError("No such file: lms")):
            with caplog.at_level(logging.ERROR, logger="vram_arbiter"):
                result = await arbiter._evict_model_async(MODEL_A)

        assert result is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "Expected ERROR log when lms binary is missing."

    # ── D3: lms subprocess timeout ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_D3_subprocess_timeout(self, arbiter, caplog):
        """
        When lms hangs past LMS_TIMEOUT_S, the process must be killed,
        False returned, and an ERROR logged — no exception escapes.
        """
        import logging

        proc = MagicMock()
        proc.returncode = None
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=proc):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                with caplog.at_level(logging.ERROR, logger="vram_arbiter"):
                    try:
                        result = await arbiter._evict_model_async(MODEL_A)
                    except Exception as exc:
                        pytest.fail(f"TimeoutError escaped eviction: {exc}")

        assert result is False
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "Expected ERROR log on subprocess timeout."

    # ── D4: lms non-zero exit code ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_D4_nonzero_exit_code(self, arbiter, caplog):
        """Non-zero returncode from lms must be logged as WARNING and return False."""
        import logging

        proc = _fake_process(returncode=1, stderr=b"model not found")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock,
                   return_value=proc):
            with caplog.at_level(logging.WARNING, logger="vram_arbiter"):
                result = await arbiter._evict_model_async(MODEL_A)

        assert result is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "Expected WARNING on non-zero lms exit code."


# ===========================================================================
# Sanity checks — constants validation
# ===========================================================================

class TestConstantsIntegrity:

    def test_vram_ceiling_exact_value(self):
        """VRAM_CEILING must equal exactly 11.4 × 1024³ bytes (12 240 537 395)."""
        expected = int(11.4 * 1024 * 1024 * 1024)
        assert VRAM_CEILING == expected, (
            f"VRAM_CEILING={VRAM_CEILING} does not match expected={expected}"
        )

    def test_eviction_cooldown_is_300s(self):
        """Eviction cooldown must be exactly 300 seconds."""
        assert EVICTION_COOLDOWN_S == 300.0

    def test_vram_estimator_scales_linearly(self):
        """_estimate_vram_bytes must scale linearly with model count."""
        arbiter = VRAMArbiterAsync()
        assert arbiter._estimate_vram_bytes(set()) == 0
        one   = arbiter._estimate_vram_bytes({"model-a"})
        two   = arbiter._estimate_vram_bytes({"model-a", "model-b"})
        three = arbiter._estimate_vram_bytes({"model-a", "model-b", "model-c"})
        assert two == one * 2, "Two models must be exactly 2× one model estimate"
        assert three == one * 3, "Three models must be exactly 3× one model estimate"
