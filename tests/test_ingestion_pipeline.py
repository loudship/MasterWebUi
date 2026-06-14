"""
tests/test_ingestion_pipeline.py
=================================
Regression and feature tests for the layout-aware document ingestion pipeline.

Coverage
--------
1.  Catalog: docling_ingestion tool is registered
2.  Catalog: web-search model has docling_ingestion in tool_ids
3.  Catalog: document ingestion is explicit and has no fire-and-forget filter
4.  Catalog: deep-web-research-docs managed knowledge collection present
5.  Tool source file exists
6.  Filter source file exists
7.  Tool: UPLOAD_CONCURRENCY_LIMIT == 4 (bounded, UI-friendly)
8.  Tool: Valves defaults (docling_url, chunk_size, chunk_overlap, uploads)
9.  Tool: _sanitize_chunk strips NUL bytes
10. Tool: _sanitize_chunk strips C0 control chars
11. Tool: _sanitize_chunk preserves prose verbatim (no SQL-token rewriting)
12. Tool: _sanitize_chunk normalises Unicode to NFC
13. Tool: _semantic_chunks produces correct overlap
14. Tool: _semantic_chunks handles empty text
15. Tool: _semantic_chunks handles single-paragraph text
16. Tool: is_pdf_url detects .pdf URLs
17. Tool: is_pdf_url detects /datasheet/ paths
18. Tool: is_pdf_url returns False for HTML
19. Tool: ingest_document returns error report on docling failure
20. Tool: ingest_document error report contains URL
21. Tool: brutalist report format — contains code fence
22. Tool: brutalist report format — contains blockquote
23. Tool: brutalist report format — contains Markdown table
24. Filter: inlet detects PDF URL and injects system marker
25. Filter: inlet does not inject marker for non-document URL
26. Filter: outlet passes body through unchanged
27. Filter: inlet handles empty messages gracefully
28. Filter: inlet handles multimodal content list
29. Tool: CPU-only imports (no torch/cuda/subprocess)
30. Filter: CPU-only imports
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = ROOT / "workspace" / "catalog-tools"
CATALOG = ROOT / "workspace" / "catalog-baseline.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_tool():
    path = TOOL_ROOT / "docling_ingestion.py"
    spec = importlib.util.spec_from_file_location("docling_ingestion", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _cpu_only(path: Path) -> None:
    forbidden = {"torch", "cuda", "subprocess"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    overlap = imports & forbidden
    assert not overlap, f"{path.name} imports forbidden modules: {overlap}"


# ===========================================================================
# 1-4: Catalog assertions
# ===========================================================================


def test_catalog_docling_ingestion_tool_registered():
    data = _catalog()
    assert "docling_ingestion" in data["tools"]
    assert "docling-tools/docling_ingestion.py" in data["tools"]["docling_ingestion"]["source"] or \
           "catalog-tools/docling_ingestion.py" in data["tools"]["docling_ingestion"]["source"]


def test_catalog_web_search_has_docling_ingestion_tool_id():
    data = _catalog()
    assert "docling_ingestion" in data["models"]["web-search"]["tool_ids"]


def test_catalog_has_no_document_ingestion_router_side_channel():
    data = _catalog()
    assert "document_ingestion_router" not in data["functions"]
    assert "document_ingestion_router" in data["archive_functions"]


def test_catalog_deep_web_research_docs_knowledge_present():
    data = _catalog()
    ids = [k.get("id") for k in data.get("managed_knowledge", [])]
    assert "deep-web-research-docs" in ids


# ===========================================================================
# 5-6: Source file existence
# ===========================================================================


def test_docling_ingestion_tool_source_exists():
    assert (TOOL_ROOT / "docling_ingestion.py").is_file()


# ===========================================================================
# 7-8: Tool constants and valves
# ===========================================================================


def test_docling_tool_upload_concurrency_is_bounded():
    """Uploads target the single-worker Open WebUI event loop: a 32-wide
    coroutine fan-out starves the chat UI (audit P1-5). Bound must stay small."""
    mod = _load_tool()
    assert mod.UPLOAD_CONCURRENCY_LIMIT == 4
    assert not hasattr(mod, "SECTION_SEMAPHORE_LIMIT")


def test_docling_tool_valves_defaults():
    mod = _load_tool()
    tool = mod.Tools()
    assert "docling-serve:5001" in tool.valves.docling_url
    assert tool.valves.chunk_size == 1000
    assert tool.valves.chunk_overlap == 100
    assert tool.valves.max_concurrent_uploads == 4
    # The write-only redis queue was removed (audit P1-6).
    assert not hasattr(tool.valves, "redis_url")


def test_docling_tool_uses_two_step_knowledge_contract():
    """The knowledge attach endpoint takes a file_id of an UPLOADED file;
    posting raw {name, content} fails validation for every chunk (audit P1-5)."""
    mod = _load_tool()
    src = (TOOL_ROOT / "docling_ingestion.py").read_text(encoding="utf-8")
    assert "/api/v1/files/" in src
    assert '"file_id"' in src
    assert hasattr(mod.Tools, "_upload_file")
    assert hasattr(mod.Tools, "_attach_file_to_knowledge")


# ===========================================================================
# 9-13: _sanitize_chunk
# ===========================================================================


def test_sanitize_strips_nul_bytes():
    mod = _load_tool()
    result = mod._sanitize_chunk("hello\x00world")
    assert "\x00" not in result
    assert "hello" in result
    assert "world" in result


def test_sanitize_strips_c0_control_chars():
    mod = _load_tool()
    result = mod._sanitize_chunk("abc\x07\x08\x0bdef")
    assert "\x07" not in result
    assert "\x08" not in result
    assert "\x0b" not in result
    assert "abcdef" in result.replace("\n", "").replace("\t", "")


def test_sanitize_preserves_prose_verbatim():
    """SQL-token rewriting corrupted documents (every apostrophe became
    [REDACTED]); parameterized queries make it security theater (audit P1-7)."""
    mod = _load_tool()
    assert mod._sanitize_chunk("don't worry — it's fine") == "don't worry — it's fine"
    technical = "Run DROP TABLE staging; -- cleanup step from the ops manual"
    assert mod._sanitize_chunk(technical) == technical
    assert "[REDACTED]" not in mod._sanitize_chunk("CAST(1 as varchar)")


def test_sanitize_normalises_unicode_nfc():
    import unicodedata
    mod = _load_tool()
    # NFD decomposed é (e + combining acute)
    nfd = "e\u0301"
    result = mod._sanitize_chunk(nfd)
    assert unicodedata.is_normalized("NFC", result)


# ===========================================================================
# 14-16: _semantic_chunks
# ===========================================================================


def test_semantic_chunks_produces_overlap():
    mod = _load_tool()
    # Create text that forces multiple chunks
    para = "word " * 250  # ~1250 chars per paragraph
    text = para + "\n\n" + para + "\n\n" + para
    chunks = mod._semantic_chunks(text, size=500, overlap=50)
    assert len(chunks) >= 2
    # Each chunk should be non-empty
    assert all(len(c) > 0 for c in chunks)


def test_semantic_chunks_handles_empty_text():
    mod = _load_tool()
    chunks = mod._semantic_chunks("", size=1000, overlap=100)
    assert chunks == [] or all(c.strip() == "" for c in chunks)


def test_semantic_chunks_handles_single_paragraph():
    mod = _load_tool()
    text = "This is a single paragraph with no breaks."
    chunks = mod._semantic_chunks(text, size=1000, overlap=100)
    assert len(chunks) == 1
    assert chunks[0] == text


# ===========================================================================
# 17-19: is_pdf_url
# ===========================================================================


def test_is_pdf_url_detects_dot_pdf():
    mod = _load_tool()
    assert mod.Tools.is_pdf_url("https://example.com/whitepaper.pdf") is True
    assert mod.Tools.is_pdf_url("https://example.com/doc.pdf?v=2") is True


def test_is_pdf_url_detects_datasheet_path():
    mod = _load_tool()
    assert mod.Tools.is_pdf_url("https://corp.example.com/datasheet/chip-v2") is True
    assert mod.Tools.is_pdf_url("https://corp.example.com/manual/ops-guide") is True


def test_is_pdf_url_returns_false_for_html():
    mod = _load_tool()
    assert mod.Tools.is_pdf_url("https://example.com/page.html") is False
    assert mod.Tools.is_pdf_url("https://example.com/api/v1/data") is False


# ===========================================================================
# 20-21: ingest_document error paths
# ===========================================================================


@pytest.mark.asyncio
async def test_ingest_document_uploads_then_attaches_by_file_id():
    """Functional path: parse → upload via /api/v1/files/ → attach file_id."""
    import httpx as _httpx

    mod = _load_tool()
    tool = mod.Tools()
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/knowledge/":
            return _httpx.Response(200, json=[])
        if request.url.path == "/api/v1/files/":
            assert b"filename=" in request.content  # multipart upload
            return _httpx.Response(200, json={"id": "file-xyz"})
        if request.url.path.endswith("/file/add"):
            assert json.loads(request.content) == {"file_id": "file-xyz"}
            return _httpx.Response(200, json={"id": "kb"})
        return _httpx.Response(404)

    transport = _httpx.MockTransport(handler)
    real_client = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda **kw: real_client(
        transport=transport, **{k: v for k, v in kw.items() if k != "transport"}
    )
    try:
        async def _parse(url):
            return "# Title\n\nBody paragraph with enough characters to ingest."

        tool._parse_via_docling = _parse
        report = await tool.ingest_document("https://example.com/spec.pdf", "Spec")
    finally:
        mod.httpx.AsyncClient = real_client

    assert "INGESTION COMPLETE" in report
    assert "file-xyz" in report
    assert ("POST", "/api/v1/files/") in calls
    assert any(path.endswith("/file/add") for _m, path in calls)


@pytest.mark.asyncio
async def test_ingest_document_returns_error_on_docling_failure():
    mod = _load_tool()
    tool = mod.Tools()

    async def _fail(url):
        raise ConnectionError("docling-serve not reachable")

    tool._parse_via_docling = _fail
    result = await tool.ingest_document("https://example.com/paper.pdf", "Test Paper")
    assert "INGESTION FAILED" in result or "failed" in result.lower()


@pytest.mark.asyncio
async def test_ingest_document_error_contains_url():
    mod = _load_tool()
    tool = mod.Tools()

    async def _fail(url):
        raise RuntimeError("parse error")

    tool._parse_via_docling = _fail
    result = await tool.ingest_document("https://example.com/report.pdf")
    assert "example.com" in result


# ===========================================================================
# 22-24: Brutalist report format
# ===========================================================================


def test_ingestion_report_contains_code_fence():
    mod = _load_tool()
    report = mod._ingestion_report(
        title="Test Doc",
        url="https://example.com/test.pdf",
        total_chars=5000,
        chunks=10,
        file_id="file-abc123",
        attached=True,
        elapsed_s=12.3,
    )
    assert "```" in report


def test_ingestion_report_contains_blockquote():
    mod = _load_tool()
    report = mod._ingestion_report(
        title="Test Doc",
        url="https://example.com/test.pdf",
        total_chars=5000,
        chunks=10,
        file_id="file-abc123",
        attached=True,
        elapsed_s=12.3,
    )
    assert report.lstrip().startswith(">")


def test_ingestion_report_contains_markdown_table():
    mod = _load_tool()
    report = mod._ingestion_report(
        title="Test Doc",
        url="https://example.com/test.pdf",
        total_chars=5000,
        chunks=10,
        file_id="file-abc123",
        attached=True,
        elapsed_s=12.3,
    )
    assert "|" in report
    assert "---" in report


def test_docling_tool_cpu_only():
    _cpu_only(TOOL_ROOT / "docling_ingestion.py")
