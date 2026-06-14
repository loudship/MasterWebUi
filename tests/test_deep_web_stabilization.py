"""
tests/test_deep_web_stabilization.py
====================================
Regression tests for the deep-web-mcp stabilization fixes (audit P2-5, P2-12):

1.  SearXNG calls fail fast (5 s default) instead of 30 s / 20 s waits.
2.  The extraction task registry prunes finished entries (was unbounded).
3.  Pruning runs before every registry seed (both /extract paths).
4.  The SSE generator cancels the background Chromium extraction when the
    client disconnects.
5.  database.py closes sessions in try/finally and rolls back on error.

The server module imports mcp/crawl4ai (container-only), so the prune logic is
extracted via AST and executed directly — same code, no import side effects.
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = (ROOT / "deep-web-mcp" / "server.py").read_text(encoding="utf-8")
MCP_TOOLS_SRC = (ROOT / "deep-web-mcp" / "mcp_tools.py").read_text(encoding="utf-8")
EXTRACTION_SRC = (ROOT / "deep-web-mcp" / "extraction.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "deep-web-mcp" / "api.py").read_text(encoding="utf-8")
DISCOVERY_SRC = (ROOT / "deep-web-mcp" / "web_discovery.py").read_text(encoding="utf-8")
DATABASE_SRC = (ROOT / "deep-web-mcp" / "database.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1: fail-fast SearXNG budgets
# ---------------------------------------------------------------------------


def test_searxng_timeout_is_five_seconds():
    assert 'SEARXNG_TIMEOUT_S: float = float(os.getenv("SEARXNG_TIMEOUT_S", "5"))' in MCP_TOOLS_SRC
    assert "timeout=SEARXNG_TIMEOUT_S" in MCP_TOOLS_SRC
    assert "timeout=30.0" not in MCP_TOOLS_SRC


def test_web_discovery_search_timeout_is_five_seconds():
    assert '"WEB_DISCOVERY_SEARCH_TIMEOUT_S", "5"' in DISCOVERY_SRC


# ---------------------------------------------------------------------------
# 2-3: bounded task registry
# ---------------------------------------------------------------------------


def _extract_prune_namespace() -> dict:
    tree = ast.parse(EXTRACTION_SRC)
    wanted = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ExtractionTask":
            wanted["cls"] = node
        if isinstance(node, ast.FunctionDef) and node.name == "prune_task_registry":
            wanted["fn"] = node
    assert "fn" in wanted, "prune_task_registry must exist in extraction.py"
    module = ast.Module(body=[wanted["cls"], wanted["fn"]], type_ignores=[])
    namespace = {
        "time": time,
        "Optional": Optional,
        "dataclass": dataclass,
        "field": field,
        "TASK_REGISTRY_TTL_S": 600.0,
        "_task_registry": {},
    }
    exec(compile(module, "<server-extract>", "exec"), namespace)
    return namespace


def test_prune_drops_only_expired_finished_tasks():
    ns = _extract_prune_namespace()
    task_cls = ns["ExtractionTask"]
    now = time.time()
    ns["_task_registry"].update(
        {
            "old-done": task_cls("old-done", "u", status="done", started_at=now - 1000),
            "old-error": task_cls("old-error", "u", status="error", started_at=now - 1000),
            "old-running": task_cls("old-running", "u", status="running", started_at=now - 1000),
            "fresh-done": task_cls("fresh-done", "u", status="done", started_at=now - 10),
        }
    )
    removed = ns["prune_task_registry"](now)
    assert removed == 2
    remaining = set(ns["_task_registry"])
    assert remaining == {"old-running", "fresh-done"}, (
        "running tasks and recent results must survive pruning"
    )


def test_prune_runs_before_every_registry_seed():
    seeds = EXTRACTION_SRC.count("_task_registry[task_id] = ExtractionTask(")
    prunes = EXTRACTION_SRC.count("prune_task_registry()")
    assert seeds >= 1
    assert prunes >= seeds, (
        f"every registry seed needs a prune call: seeds={seeds}, prunes={prunes}"
    )


# ---------------------------------------------------------------------------
# 4: background extraction cancelled on client disconnect
# ---------------------------------------------------------------------------


def test_sse_generator_cancels_orphaned_extraction():
    generator_section = API_SRC.split("async def _sse_generator", 1)[1]
    finally_block = generator_section.split("finally:", 1)
    assert len(finally_block) == 2, "_sse_generator needs a finally guard"
    assert "bg_task.cancel()" in finally_block[1].split("# Retrieve final result")[0], (
        "client disconnect must cancel the Chromium-backed extraction task"
    )
    assert "except asyncio.CancelledError:" in generator_section


# ---------------------------------------------------------------------------
# 5: credential vault session hygiene
# ---------------------------------------------------------------------------


def test_database_sessions_are_closed_in_finally():
    for fn_name in ("save_credentials", "get_credentials"):
        body = DATABASE_SRC.split(f"def {fn_name}(", 1)[1].split("\ndef ", 1)[0]
        assert "finally:" in body, f"{fn_name} must close its session in finally"
        assert "db.close()" in body
    save_body = DATABASE_SRC.split("def save_credentials(", 1)[1].split("\ndef ", 1)[0]
    assert "db.rollback()" in save_body, "failed writes must roll back"


def test_database_comment_no_longer_claims_aes_256():
    assert "AES-256" not in DATABASE_SRC, (
        "Fernet is AES-128-CBC + HMAC-SHA256; the comment must not overstate it"
    )
