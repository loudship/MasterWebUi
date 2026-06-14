"""
policy.py — Shared policy constants for the deep-web-mcp service.

Single source of truth for limits, timeouts, and thresholds so the values
stay consistent across extraction.py, research.py, mcp_tools.py, and api.py.
Override any constant via the corresponding environment variable.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Content size limits
# ---------------------------------------------------------------------------
MAX_CHARS: int = int(os.getenv("DEEP_WEB_MAX_CHARS", "20000"))
TRUNCATION_SENTINEL: str = "[[TRUNCATED: output budget reached]]"

# ---------------------------------------------------------------------------
# Multi-hop research defaults
# ---------------------------------------------------------------------------
RESEARCH_MAX_HOPS: int = int(os.getenv("RESEARCH_MAX_HOPS", "4"))
RESEARCH_TOTAL_BUDGET_S: float = float(os.getenv("RESEARCH_TOTAL_BUDGET_S", "90"))
RESEARCH_PER_HOP_TIMEOUT_S: float = float(os.getenv("RESEARCH_PER_HOP_TIMEOUT_S", "30"))
RESEARCH_MIN_ACTIVE_SOURCES: int = int(os.getenv("RESEARCH_MIN_ACTIVE_SOURCES", "2"))
RESEARCH_MIN_EVIDENCE_CHARS: int = int(os.getenv("RESEARCH_MIN_EVIDENCE_CHARS", "400"))
RESEARCH_MAX_SOURCES: int = int(os.getenv("RESEARCH_MAX_SOURCES", "8"))
RESEARCH_MAX_ITERATIONS: int = int(os.getenv("RESEARCH_MAX_ITERATIONS", "3"))

# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------
TASK_REGISTRY_TTL_S: float = float(os.getenv("TASK_REGISTRY_TTL_S", "600"))

# ---------------------------------------------------------------------------
# Browser extraction
# ---------------------------------------------------------------------------
PAGE_TIMEOUT_MS: int = int(os.getenv("PAGE_TIMEOUT_MS", "45000"))
ALLOW_PUBLIC_TARGETS: bool = os.getenv("ALLOW_PUBLIC_TARGETS", "false").lower() == "true"
ALLOWED_TARGET_HOSTS: frozenset[str] = frozenset(
    host.strip().lower()
    for host in os.getenv("ALLOWED_TARGET_HOSTS", "crawl4ai,searxng,browserless").split(",")
    if host.strip()
)
