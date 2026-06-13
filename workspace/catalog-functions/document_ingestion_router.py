"""
title: Document Ingestion Router
author: Local Operations
version: 1.0.0
description: |
  Inlet filter that detects PDF/binary document URLs embedded in user messages or
  tool results. When a document URL is found, it dispatches the URL to the
  docling_ingestion tool asynchronously so that heavy parsing runs in parallel
  with the ongoing web crawl, never blocking the chat response loop.

  Priority 40 — runs before agentic_react_loop (50) and brutalist_artifact_formatter (80).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("document_ingestion_router")

# Patterns that identify document URLs requiring layout-aware parsing
_DOC_URL_RE = re.compile(
    r"https?://\S+(?:"
    r"\.pdf"
    r"|\.docx?"
    r"|\.xlsx?"
    r"|\.pptx?"
    r"|/datasheet/"
    r"|/manual/"
    r"|/whitepaper/"
    r"|/report/"
    r")(?:\?[^\s\"'<>]*)?\b",
    re.IGNORECASE,
)

# Redis queue key shared with docling_ingestion.py
_REDIS_QUEUE_KEY = "ingestion:queue"


class Filter:

    class Valves(BaseModel):
        priority: int = Field(default=40, description="Filter priority in outlet chain.")
        enabled: bool = Field(default=True, description="Enable/disable document interception.")
        docling_url: str = Field(
            default="http://docling-serve:5001",
            description="Docling-serve container URL.",
        )
        redis_url: str = Field(
            default="redis://redis-cache:6379/0",
            description="Redis cache URL.",
        )
        open_webui_url: str = Field(
            default="http://open-webui:8080",
            description="Open WebUI internal URL.",
        )
        open_webui_api_key: str = Field(
            default="",
            description="Bearer token for knowledge API.",
        )
        knowledge_id: str = Field(
            default="deep-web-research-docs",
            description="Target knowledge collection ID.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._pending_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Inlet — intercept user messages before they reach the model
    # ------------------------------------------------------------------

    def inlet(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        Scan incoming messages for document URLs. Dispatch async ingestion tasks
        for any detected documents without blocking the inlet pipeline.
        """
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        found_urls: list[str] = []

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                found_urls.extend(_DOC_URL_RE.findall(content))
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        found_urls.extend(_DOC_URL_RE.findall(part["text"]))

        if found_urls:
            # De-duplicate preserving order
            seen: set[str] = set()
            unique_urls = [u for u in found_urls if not (u in seen or seen.add(u))]  # type: ignore[func-returns-value]

            for url in unique_urls:
                logger.info("[DOC_ROUTER] Dispatching ingestion for: %s", url)
                self._dispatch_ingestion(url)

            # Inject a system note so the model knows docs are being processed
            if not any(
                m.get("role") == "system" and "[doc-ingestion-pending]" in m.get("content", "")
                for m in messages
            ):
                body = dict(body)
                body["messages"] = [
                    {
                        "role": "system",
                        "content": (
                            "[doc-ingestion-pending] "
                            f"{len(unique_urls)} document(s) are being processed through "
                            "docling-serve for layout extraction. Results will appear in "
                            "the knowledge base as `deep-web-research-docs`."
                        ),
                    }
                ] + list(messages)

        return body

    # ------------------------------------------------------------------
    # Outlet — pass through unchanged (ingestion is fire-and-forget)
    # ------------------------------------------------------------------

    def outlet(self, body: dict[str, Any]) -> dict[str, Any]:
        return body

    # ------------------------------------------------------------------
    # Internal: fire-and-forget ingestion dispatch
    # ------------------------------------------------------------------

    def _dispatch_ingestion(self, url: str) -> None:
        """Schedule ingestion as a background asyncio task."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                task = loop.create_task(self._ingest_url(url))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
            else:
                # Synchronous fallback — push URL directly to redis queue
                self._sync_push_url(url)
        except RuntimeError:
            self._sync_push_url(url)

    async def _ingest_url(self, url: str) -> None:
        """Async ingestion: import the tool and call ingest_document."""
        try:
            import importlib.util
            from pathlib import Path
            spec = importlib.util.spec_from_file_location(
                "docling_ingestion",
                Path(__file__).parent.parent / "catalog-tools" / "docling_ingestion.py",
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                tool = mod.Tools()
                tool.valves.docling_url = self.valves.docling_url
                tool.valves.redis_url = self.valves.redis_url
                tool.valves.open_webui_url = self.valves.open_webui_url
                tool.valves.open_webui_api_key = self.valves.open_webui_api_key
                tool.valves.knowledge_id = self.valves.knowledge_id
                report = await tool.ingest_document(url)
                logger.info("[DOC_ROUTER] Ingestion complete for %s: %s", url, report[:120])
        except Exception as exc:
            logger.error("[DOC_ROUTER] Ingestion failed for %s: %s", url, exc)

    def _sync_push_url(self, url: str) -> None:
        """Synchronous fallback: log the URL for deferred processing."""
        logger.info("[DOC_ROUTER] Queued for deferred ingestion (no event loop): %s", url)
