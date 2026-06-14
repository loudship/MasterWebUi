"""
vram_arbiter_daemon.py — Async VRAM Hardware Arbiter (v2)
==========================================================

Non-blocking asyncio background daemon that:

  1. Polls the local LM Studio /api/v1/models endpoint at a strict 1000 ms interval.
  2. Tracks active context array allocations and their estimated VRAM footprints.
  3. Compares total allocated VRAM against the VRAM_CEILING threshold
     (11.4 GB out of 12 GB physical capacity).
  4. When VRAM_CEILING is met or exceeded, triggers a cache eviction subprocess
     via the lms CLI with an explicit TTL of 300 seconds on the LRU model.
  5. All network polling routines are wrapped in a broad try/except that catches
     connection refusals and socket errors silently.  If the endpoint is under
     extreme computational load, a warning is logged and the daemon passes to the
     next interval without crashing.

Changes from v1
---------------
- Replaced blocking `requests` + `time.sleep` loop with a pure asyncio event
  loop using `aiohttp` and `asyncio.sleep`.
- Polling interval tightened from 5 000 ms → 1 000 ms as specified.
- VRAM_CEILING threshold added (11.4 GiB).  The existing LRU eviction logic
  now triggers on ceiling breach rather than purely on model discovery.
- `subprocess.run()` replaced with `asyncio.create_subprocess_exec()` so the
  eviction shell call does not block the event loop.
- Comprehensive exception hierarchy retained: connection refusal, socket errors,
  JSON decode failures, and unexpected exceptions are all caught and logged at
  the correct severity without propagating.

Architecture
------------
  asyncio event loop
    └── _arbiter_loop()           ← 1000 ms periodic driver (asyncio.sleep)
          └── _poll_vram_state()  ← async aiohttp GET /api/v1/models
                └── _evict_model_async()  ← asyncio subprocess lms CLI
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Optional

import aiohttp

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("vram_arbiter")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

LM_STUDIO_API: str = os.environ.get(
    "LM_STUDIO_API",
    "http://localhost:4321/api/v1/models",
)

# Strict 1000 ms polling rate (non-blocking asyncio.sleep)
POLL_INTERVAL_S: float = 1.0

# VRAM ceiling: 11.4 GiB (11.4 × 1024³ bytes)
VRAM_CEILING: int = int(
    os.environ.get("VRAM_CEILING", str(int(11.4 * 1024 * 1024 * 1024)))
)

# Per-model eviction cooldown to prevent rapid restart spam
EVICTION_COOLDOWN_S: float = 300.0

# Estimated VRAM per context slot (bytes).  LM Studio does not expose per-model
# VRAM usage via the /v1/models endpoint, so we use a conservative default.
# Override via env var if a different heuristic is preferred.
VRAM_PER_MODEL_ESTIMATE: int = int(
    float(os.environ.get("VRAM_PER_MODEL_ESTIMATE_GB", "5.7")) * 1024 * 1024 * 1024
)

# aiohttp request timeout (seconds)
HTTP_TIMEOUT_S: float = 5.0

# lms subprocess timeout (seconds)
LMS_TIMEOUT_S:  float = 15.0

# VRAM held back for the desktop compositor / browser UI when the ceiling is
# derived from measured NVML totals (bytes).
UI_VRAM_RESERVE_BYTES: int = int(
    float(os.environ.get("UI_VRAM_RESERVE_GB", "1.5")) * 1024 * 1024 * 1024
)


def _extract_loaded_models(data: dict) -> set[str]:
    """Return only model keys that LM Studio reports as currently loaded.

    Supported response shapes, newest first:
      1. /api/v0/models REST schema: {"data": [{"id", "state": "loaded"|...}]}
      2. {"models": [{"key", "loaded_instances": [...]}]}
      3. Legacy OpenAI-compatible /v1/models: {"data": [{"id"}]} — the endpoint
         only lists loaded models, so every entry counts.
    """
    models = data.get("models")
    if isinstance(models, list):
        return {
            model["key"]
            for model in models
            if model.get("key") and model.get("loaded_instances")
        }

    entries = [model for model in data.get("data", []) if isinstance(model, dict)]
    if any("state" in model for model in entries):
        return {
            model["id"]
            for model in entries
            if model.get("id") and model.get("state") == "loaded"
        }

    return {model["id"] for model in entries if model.get("id")}


# ---------------------------------------------------------------------------
# lms binary resolver (preserved from v1, unchanged)
# ---------------------------------------------------------------------------

def _find_lms_binary() -> str:
    """Find the lms executable: PATH → LOCALAPPDATA fallback → USERPROFILE cache."""
    lms = shutil.which("lms")
    if lms:
        return lms

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidate = os.path.join(
            local_appdata, "Programs", "lm-studio",
            "resources", "app", "resources", "cli", "bin", "lms.exe",
        )
        if os.path.isfile(candidate):
            return candidate

    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        candidate = os.path.join(
            user_profile, ".cache", "lm-studio", "bin", "lms.exe",
        )
        if os.path.isfile(candidate):
            return candidate

    return "lms"   # last resort — rely on PATH at call time


# ===========================================================================
# VRAMArbiterAsync
# ===========================================================================

class VRAMArbiterAsync:
    """
    Non-blocking async VRAM arbiter.

    State tracking
    --------------
    active_models   : set[str]  — model IDs currently resident in VRAM.
    resident_since  : dict[str, float]  — Unix timestamp of first observation.
    last_eviction   : dict[str, float]  — Unix timestamp of last eviction attempt.
    """

    def __init__(self) -> None:
        self.active_models:  set[str]       = set()
        self.resident_since: dict[str, float] = {}
        self.last_eviction:  dict[str, float] = {}
        self._lms_bin: str = _find_lms_binary()
        self._nvml_handle = None
        self._nvml_unavailable: bool = False

    # ------------------------------------------------------------------
    # Measured VRAM via NVML (optional, host-side only)
    # ------------------------------------------------------------------

    def _measured_vram(self) -> Optional[tuple[int, int]]:
        """Return (used_bytes, total_bytes) from NVML, or None when NVML is
        unavailable (pynvml not installed, or running off-host)."""
        if self._nvml_unavailable:
            return None
        try:
            import pynvml
            if self._nvml_handle is None:
                pynvml.nvmlInit()
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
            return int(info.used), int(info.total)
        except Exception as exc:
            self._nvml_unavailable = True
            logger.warning(
                "[ARBITER] NVML unavailable (%s: %s) — falling back to the "
                "per-model VRAM estimate. Install pynvml on the host for "
                "measured enforcement.",
                type(exc).__name__, exc,
            )
            return None

    # ------------------------------------------------------------------
    # Eviction cooldown guard
    # ------------------------------------------------------------------

    def _should_evict(self, model_key: str) -> bool:
        now = time.monotonic()
        last = self.last_eviction.get(model_key, 0.0)
        return (now - last) >= EVICTION_COOLDOWN_S

    # ------------------------------------------------------------------
    # Async eviction subprocess  (non-blocking)
    # ------------------------------------------------------------------

    async def _evict_model_async(self, model_key: str) -> bool:
        """
        Unload *model_key* from VRAM via the lms CLI.

        `lms unload` frees the memory immediately. (The previous
        implementation ran `lms load --ttl 300`, which (re)loads the model and
        merely schedules an idle eviction — the opposite of reclaiming VRAM at
        breach time.)

        Uses asyncio.create_subprocess_exec so the call does not block the
        event loop during token generation or heavy I/O.
        Returns True on success, False on any failure.
        """
        if not self._should_evict(model_key):
            logger.debug("[ARBITER] Eviction cooldown active for %r — skipping.", model_key)
            return True   # not an error; cooldown is expected behaviour

        logger.info(
            "[ARBITER] VRAM ceiling breach — unloading %r via %s",
            model_key, self._lms_bin,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                self._lms_bin, "unload", model_key,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=LMS_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                logger.error(
                    "[ARBITER] lms TTL command timed out after %.0fs for %r.",
                    LMS_TIMEOUT_S, model_key,
                )
                return False

            if proc.returncode == 0:
                logger.info(
                    "[ARBITER] Unloaded %r successfully.  stdout: %s",
                    model_key, stdout.decode(errors="replace").strip(),
                )
                self.last_eviction[model_key] = time.monotonic()
                return True
            else:
                logger.warning(
                    "[ARBITER] lms command returned code %d for %r.  stderr: %s",
                    proc.returncode, model_key,
                    stderr.decode(errors="replace").strip(),
                )
                return False

        except FileNotFoundError:
            logger.error(
                "[ARBITER] CRITICAL — 'lms' binary not found at %r. "
                "Ensure LM Studio CLI is installed and on PATH.",
                self._lms_bin,
            )
            return False
        except Exception as exc:
            logger.error("[ARBITER] Unexpected error evicting %r: %s", model_key, exc)
            return False

    # ------------------------------------------------------------------
    # VRAM footprint estimator
    # ------------------------------------------------------------------

    def _estimate_vram_bytes(self, models: set[str]) -> int:
        """
        Estimate current total VRAM allocation from active model set.
        Uses VRAM_PER_MODEL_ESTIMATE bytes per model as a conservative heuristic
        (LM Studio does not expose per-model byte usage directly).
        """
        return len(models) * VRAM_PER_MODEL_ESTIMATE

    # ------------------------------------------------------------------
    # Core async polling coroutine
    # ------------------------------------------------------------------

    async def _poll_vram_state(self, session: aiohttp.ClientSession) -> None:
        """
        Query LM Studio /api/v1/models and apply VRAM ceiling arbitration.

        All network errors are caught silently.  JSON parse failures and
        unexpected exceptions are logged at WARNING/ERROR without propagating,
        so the outer loop continues on the next 1000 ms tick.
        """
        try:
            async with session.get(
                LM_STUDIO_API,
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "[ARBITER] LM Studio returned HTTP %d — skipping this interval.",
                        resp.status,
                    )
                    return

                try:
                    data = await resp.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                    logger.warning("[ARBITER] JSON parse failure: %s — passing.", exc)
                    return

        # ── Silent catch: connection refusal / socket errors ───────────────
        except (
            aiohttp.ClientConnectorError,       # connection refused / no route
            aiohttp.ServerDisconnectedError,    # server dropped mid-response
            aiohttp.ClientOSError,              # OS-level socket error
            asyncio.TimeoutError,               # endpoint under extreme load
        ) as exc:
            logger.warning(
                "[ARBITER] Endpoint unreachable or timed out (%s: %s) — "
                "passing to next interval without crash.",
                type(exc).__name__, exc,
            )
            return

        except Exception as exc:
            # Broad safety net — never crash the daemon loop
            logger.error(
                "[ARBITER] Unexpected polling exception (%s: %s) — passing.",
                type(exc).__name__, exc,
            )
            return

        # ------------------------------------------------------------------
        # Process model list
        # ------------------------------------------------------------------
        current_time = time.monotonic()
        current_models = _extract_loaded_models(data)

        # ── Track new model appearances ────────────────────────────────────
        for model in current_models:
            if model not in self.resident_since:
                self.resident_since[model] = current_time
                logger.info("[ARBITER] Model loaded into VRAM: %r", model)

        # ── Prune departed models ──────────────────────────────────────────
        for model in list(self.resident_since):
            if model not in current_models:
                del self.resident_since[model]
                logger.info("[ARBITER] Model evicted/unloaded: %r", model)

        self.active_models = current_models

        # ── VRAM ceiling check ─────────────────────────────────────────────
        # Prefer measured bytes from NVML; fall back to the per-model estimate.
        # When totals are measurable the ceiling also honours the UI reserve so
        # the compositor/browser never get squeezed out of the 12 GB card.
        measured = self._measured_vram()
        if measured is not None:
            estimated_vram, total_bytes = measured
            ceiling = min(VRAM_CEILING, max(0, total_bytes - UI_VRAM_RESERVE_BYTES))
        else:
            estimated_vram = self._estimate_vram_bytes(current_models)
            ceiling = VRAM_CEILING
        ceiling_pct = estimated_vram / ceiling * 100 if ceiling else 0

        logger.debug(
            "[ARBITER] Active models: %d  |  VRAM (%s): %.2f GiB  |  "
            "Ceiling: %.2f GiB  |  Usage: %.1f%%",
            len(current_models),
            "measured" if measured is not None else "estimated",
            estimated_vram / (1024 ** 3),
            ceiling / (1024 ** 3),
            ceiling_pct,
        )

        if estimated_vram >= ceiling:
            logger.warning(
                "[ARBITER] ⚠ VRAM CEILING BREACH — %.2f GiB ≥ ceiling %.2f GiB. "
                "Unloading the least-recently-loaded model.",
                estimated_vram / (1024 ** 3),
                ceiling / (1024 ** 3),
            )

            # Evict the LRU (oldest-resident) model to reclaim memory
            if current_models:
                oldest_model = min(
                    current_models,
                    key=lambda m: self.resident_since.get(m, current_time),
                )
                resident_age = current_time - self.resident_since.get(oldest_model, current_time)
                logger.warning(
                    "[ARBITER] LRU eviction target: %r (resident for %.0fs).",
                    oldest_model, resident_age,
                )
                await self._evict_model_async(oldest_model)

        # ── LRU collision guard (multiple simultaneous models, no ceiling breach) ─
        elif len(current_models) > 1:
            oldest_model = min(
                current_models,
                key=lambda m: self.resident_since.get(m, current_time),
            )
            logger.warning(
                "[ARBITER] COLLISION RISK: %d active models %s. "
                "Evicting oldest resident: %r.",
                len(current_models), current_models, oldest_model,
            )
            await self._evict_model_async(oldest_model)

    # ------------------------------------------------------------------
    # Main daemon loop  (1000 ms non-blocking asyncio.sleep)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Primary async daemon loop.

        Creates a persistent aiohttp.ClientSession for connection-pool reuse
        across polling intervals.  Each 1000 ms tick calls _poll_vram_state()
        without blocking the event loop.
        """
        logger.info("=" * 60)
        logger.info("  Async VRAM Hardware Arbiter Daemon v2")
        logger.info("  Endpoint   : %s", LM_STUDIO_API)
        logger.info("  Interval   : %.0f ms", POLL_INTERVAL_S * 1000)
        logger.info("  VRAM ceil  : %.2f GiB", VRAM_CEILING / (1024 ** 3))
        logger.info("  Per-model  : %.2f GiB (estimate)", VRAM_PER_MODEL_ESTIMATE / (1024 ** 3))
        logger.info("  Evict cool : %.0f s", EVICTION_COOLDOWN_S)
        logger.info("  lms binary : %s", self._lms_bin)
        logger.info("=" * 60)

        connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                tick_start = asyncio.get_event_loop().time()

                # Poll VRAM state — all exceptions handled inside
                await self._poll_vram_state(session)

                # Sleep for the remainder of the 1000 ms window
                elapsed   = asyncio.get_event_loop().time() - tick_start
                remaining = max(0.0, POLL_INTERVAL_S - elapsed)
                await asyncio.sleep(remaining)


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    arbiter = VRAMArbiterAsync()
    if shutil.which("lms") is None and os.name != "nt":
        logger.critical(
            "[ARBITER] 'lms' CLI is not on PATH and this is not a Windows host. "
            "Eviction WILL fail from inside a container: the LM Studio CLI lives "
            "on the host. Run this daemon host-side (scheduled task / service) "
            "so it can reach lms.exe."
        )
    try:
        asyncio.run(arbiter.run())
    except KeyboardInterrupt:
        logger.info("[ARBITER] Keyboard interrupt received — daemon shutting down cleanly.")


if __name__ == "__main__":
    main()
