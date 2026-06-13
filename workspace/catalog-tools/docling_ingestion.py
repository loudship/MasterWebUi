"""
title: Docling Layout-Aware Document Ingestion
author: Local Operations
version: 2.0.0
description: |
  Intercepts PDF/binary document URLs discovered during web crawls, routes them
  through the containerised docling-serve parser (port 5001) for complete layout
  extraction (tables → Markdown), and registers the parsed document with the
  Open WebUI knowledge base using the real two-step contract:

    1. POST /api/v1/files/            (multipart upload of the parsed Markdown)
    2. POST /api/v1/knowledge/{id}/file/add   ({"file_id": ...})

  Open WebUI's own splitter chunks and embeds the file server-side, so the
  document is uploaded once instead of being fanned out chunk-by-chunk into the
  single-worker UI process.

  Sanitisation: NUL bytes, C0/C1 control characters, and malformed Unicode are
  scrubbed before upload. Content is otherwise preserved verbatim — the
  database layer uses parameterized queries, so rewriting SQL-looking prose
  (apostrophes, comments, DDL keywords in documentation) only corrupts data.

  Brutalist output: the final ingestion report uses OLED-black code fences,
  cyan blockquotes, and monospace table borders to honour the active design system.

requirements: httpx
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("docling_ingestion")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Concurrent document uploads. These are I/O-bound coroutines hitting the
# single-worker Open WebUI event loop — a small bound protects UI latency;
# fanning out 32-wide only starves the chat loop.
UPLOAD_CONCURRENCY_LIMIT: int = 4

CHUNK_SIZE: int = 1000
CHUNK_OVERLAP: int = 100

DEFAULT_KNOWLEDGE_ID: str = "deep-web-research-docs"

# Docling /convert endpoint payload defaults
DOCLING_TO_FORMATS: list[str] = ["md"]
DOCLING_OPTIONS: dict[str, Any] = {
    "to_formats": DOCLING_TO_FORMATS,
    "from_doc_convert_options": {
        "enable_layout": True,
        "table_mode": "accurate",
        "image_placeholder": "<!-- figure -->",
    },
}

# Document URL patterns that trigger ingestion
_PDF_RE = re.compile(
    r"\.pdf($|\?|#)"
    r"|content-type=application/pdf"
    r"|/document/"
    r"|/datasheet/"
    r"|/manual/",
    re.IGNORECASE,
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


# ---------------------------------------------------------------------------
# Tools class (Open WebUI tool interface)
# ---------------------------------------------------------------------------

class Tools:

    class Valves(BaseModel):
        docling_url: str = Field(
            default="http://docling-serve:5001",
            description="Base URL of the docling-serve container.",
        )
        open_webui_url: str = Field(
            default="http://open-webui:8080",
            description="Internal Open WebUI base URL for knowledge API calls.",
        )
        open_webui_api_key: str = Field(
            default="",
            description="Bearer token for Open WebUI knowledge API.",
        )
        knowledge_id: str = Field(
            default=DEFAULT_KNOWLEDGE_ID,
            description="Target knowledge collection ID.",
        )
        max_concurrent_uploads: int = Field(
            default=UPLOAD_CONCURRENCY_LIMIT,
            ge=1,
            le=8,
            description="Max concurrent document uploads into Open WebUI.",
        )
        chunk_size: int = Field(default=CHUNK_SIZE, ge=200, le=4000)
        chunk_overlap: int = Field(default=CHUNK_OVERLAP, ge=0, le=500)
        docling_timeout_s: int = Field(default=300, ge=30, le=600)
        http_timeout_s: int = Field(default=60, ge=10, le=300)

    def __init__(self):
        self.valves = self.Valves()
        self._upload_sem: asyncio.Semaphore | None = None

    @property
    def _sem(self) -> asyncio.Semaphore:
        if self._upload_sem is None:
            self._upload_sem = asyncio.Semaphore(self.valves.max_concurrent_uploads)
        return self._upload_sem

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.valves.open_webui_api_key:
            headers["Authorization"] = f"Bearer {self.valves.open_webui_api_key}"
        return headers

    async def _resolve_knowledge_id(self) -> str:
        """
        Query Open WebUI /api/v1/knowledge/ to find the collection named
        'Knowledge - Research - Web Search Reports' and return its UUID.
        Fallback to self.valves.knowledge_id if not found or on error.
        """
        target_name = "Knowledge - Research - Web Search Reports"
        try:
            async with httpx.AsyncClient(
                base_url=self.valves.open_webui_url,
                headers=self._headers(),
                timeout=self.valves.http_timeout_s,
            ) as client:
                resp = await client.get("/api/v1/knowledge/")
                resp.raise_for_status()
                data = resp.json()

                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("items", [])

                for item in items:
                    if item.get("name") == target_name:
                        logger.info("Dynamically resolved knowledge UUID for '%s': %s", target_name, item.get("id"))
                        return item.get("id")
        except Exception as exc:
            logger.warning("Failed to dynamically resolve knowledge collection UUID: %s", exc)

        return self.valves.knowledge_id

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def ingest_document(
        self,
        url: str,
        title: str = "",
        *,
        __event_emitter__=None,
    ) -> str:
        """
        Full pipeline: fetch → parse (docling) → sanitise → upload (files API)
        → attach to knowledge (server-side chunking + embedding).

        Returns a brutalist Markdown ingestion report.
        """
        start = time.time()
        url = url.strip()
        title = title.strip() or _title_from_url(url)

        _emit(__event_emitter__, "status", f"Starting ingestion: {title}")

        # Resolve target knowledge collection ID dynamically
        knowledge_id = await self._resolve_knowledge_id()

        # 1. Parse via docling-serve
        try:
            markdown_text = await self._parse_via_docling(url)
        except Exception as exc:
            return _error_report(title, url, f"Docling parse failed: {exc}")

        _emit(__event_emitter__, "status", "Sanitising parsed document…")

        # 2. Sanitise (NUL/control characters only — content stays verbatim)
        clean_text = _sanitize_chunk(markdown_text)
        if len(clean_text.strip()) < 20:
            return _error_report(title, url, "No valid content extracted after sanitisation.")
        chunk_count = len(_semantic_chunks(clean_text, self.valves.chunk_size, self.valves.chunk_overlap))

        # 3. Upload once; Open WebUI chunks and embeds server-side.
        _emit(__event_emitter__, "status", "Uploading document to knowledge base…")
        try:
            async with self._sem:
                file_id = await self._upload_file(title, url, clean_text)
                attached = await self._attach_file_to_knowledge(knowledge_id, file_id)
        except Exception as exc:
            return _error_report(title, url, f"Knowledge registration failed: {exc}")

        elapsed = round(time.time() - start, 1)
        return _ingestion_report(
            title=title,
            url=url,
            total_chars=len(clean_text),
            chunks=chunk_count,
            file_id=file_id,
            attached=attached,
            knowledge_id=knowledge_id,
            elapsed_s=elapsed,
        )

    # ------------------------------------------------------------------
    # Docling parse
    # ------------------------------------------------------------------

    async def _parse_via_docling(self, url: str) -> str:
        """POST to docling-serve /api/v1/document/convert and return Markdown."""
        payload = {
            "http_source": {"url": url},
            "options": DOCLING_OPTIONS,
        }
        async with httpx.AsyncClient(
            base_url=self.valves.docling_url,
            timeout=self.valves.docling_timeout_s,
        ) as client:
            resp = await client.post("/api/v1/document/convert", json=payload)
            resp.raise_for_status()
            data = resp.json()

        # docling-serve returns {"document": {"md_content": "..."}}
        md = (
            data.get("document", {}).get("md_content")
            or data.get("output", {}).get("md_content")
            or data.get("md_content")
            or ""
        )
        if not md:
            raise ValueError(f"docling-serve returned no md_content: {list(data.keys())}")
        return md

    # ------------------------------------------------------------------
    # Open WebUI knowledge contract: upload file, then attach by file_id
    # ------------------------------------------------------------------

    async def _upload_file(self, title: str, source_url: str, content: str) -> str:
        """POST /api/v1/files/ (multipart) and return the new file id.

        The previous implementation POSTed {name, content} JSON straight to
        /knowledge/{id}/file/add, which expects a file_id of an UPLOADED file —
        every request failed validation and nothing was ever embedded.
        """
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:120] or "document"
        if not filename.lower().endswith(".md"):
            filename += ".md"
        async with httpx.AsyncClient(
            base_url=self.valves.open_webui_url,
            headers=self._headers(),
            timeout=self.valves.http_timeout_s,
        ) as client:
            resp = await client.post(
                "/api/v1/files/",
                files={"file": (filename, content.encode("utf-8"), "text/markdown")},
            )
            resp.raise_for_status()
            data = resp.json()
        file_id = data.get("id")
        if not file_id:
            raise ValueError(f"files API returned no id: {list(data)}")
        logger.info("[INGESTION] Uploaded %r as file %s (source=%s)", title, file_id, source_url)
        return file_id

    async def _attach_file_to_knowledge(self, knowledge_id: str, file_id: str) -> bool:
        """POST /api/v1/knowledge/{id}/file/add with the uploaded file_id."""
        async with httpx.AsyncClient(
            base_url=self.valves.open_webui_url,
            headers=self._headers(),
            timeout=self.valves.http_timeout_s,
        ) as client:
            resp = await client.post(
                f"/api/v1/knowledge/{knowledge_id}/file/add",
                json={"file_id": file_id},
            )
            resp.raise_for_status()
        return True

    # ------------------------------------------------------------------
    # Convenience: detect PDF URL
    # ------------------------------------------------------------------

    @staticmethod
    def is_pdf_url(url: str) -> bool:
        """Return True if url appears to reference a PDF or binary document."""
        return bool(_PDF_RE.search(url))


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------

def _sanitize_chunk(text: str) -> str:
    """Strip control characters and normalise Unicode; preserve content.

    PostgreSQL rejects NUL bytes — everything else in the document is data.
    SQL-token rewriting was removed: parameterized queries make it pointless,
    and it corrupted prose (every apostrophe became [REDACTED]).
    """
    # NFC normalise to eliminate combining-character attacks
    text = unicodedata.normalize("NFC", text)
    # Strip C0/C1 control characters (keep \t and \n)
    text = _CONTROL_RE.sub("", text)
    # Collapse trailing whitespace per line
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Chunking (informational: server-side splitter does the real chunking)
# ---------------------------------------------------------------------------

def _semantic_chunks(text: str, size: int, overlap: int) -> list[str]:
    """Split text into overlapping semantic chunks on paragraph boundaries."""
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > size and current:
            chunks.append(current.strip())
            # carry-over overlap
            words = current.split()
            carry = " ".join(words[max(0, len(words) - overlap // 5):])
            current = carry + "\n\n" + para
        else:
            current = (current + "\n\n" + para) if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _title_from_url(url: str) -> str:
    try:
        path = urlparse(url).path
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return name or url[:80]
    except Exception:
        return url[:80]


def _emit(emitter, event_type: str, data: str) -> None:
    if emitter is None:
        return
    try:
        asyncio.ensure_future(
            emitter({"type": event_type, "data": {"description": data, "done": False}})
        )
    except Exception:
        pass


def _ingestion_report(
    *,
    title: str,
    url: str,
    total_chars: int,
    chunks: int,
    file_id: str,
    attached: bool,
    knowledge_id: str = DEFAULT_KNOWLEDGE_ID,
    elapsed_s: float,
) -> str:
    """Brutalist Markdown report — OLED-black fences, cyan blockquotes, monospace tables."""
    return f"""> **📥 INGESTION COMPLETE** — `{title}`

```
┌─────────────────────────────────────────────────────┐
│  SOURCE    {url[:56]:56s}│
│  CHARS     {total_chars:<8d}  CHUNKS    {chunks:<8d}         │
│  FILE ID   {file_id[:40]:40s}             │
│  ELAPSED   {elapsed_s:<8.1f}s                               │
└─────────────────────────────────────────────────────┘
```

> `docling-serve:5001` → parsed layout + tables into Markdown  \\
> `files API` → uploaded as `{file_id[:24]}`  \\
> `knowledge/{knowledge_id}` → server-side chunking + embedding {'✅ attached' if attached else '❌ attach failed'}

| Metric | Value |
|---|---|
| Layout extraction | ✅ complete |
| Table conversion | ✅ Markdown tables |
| Sanitisation | ✅ control chars stripped, content preserved |
| Knowledge attach | {'✅ ok' if attached else '❌ failed'} |
"""


def _error_report(title: str, url: str, reason: str) -> str:
    return f"""> **❌ INGESTION FAILED** — `{title}`

```
SOURCE  {url[:60]}
REASON  {reason[:120]}
```
"""
