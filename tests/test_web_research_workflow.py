"""Contracts for the server-owned web research workflow."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = ROOT / "workspace" / "catalog-tools"
CATALOG = ROOT / "workspace" / "catalog-baseline.yaml"


def _catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _load_tool(name: str):
    path = TOOL_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Tools()


def _cpu_only_check(path: Path) -> None:
    forbidden = {"torch", "cuda", "subprocess", "requests", "aiohttp", "socket"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports |= {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imports & forbidden


def test_web_search_is_a_declarative_workflow_facade():
    workflow = _catalog()["models"]["web-search"]
    assert workflow["clone_from"] == "qwen35"
    assert workflow["catalog"]["kind"] == "workflow"
    assert workflow["catalog"]["orchestrator"] == "deep-web-mcp"
    assert workflow["catalog"]["entrypoint"] == "/?model=web-search"
    assert workflow["name"].startswith("Workflow - ")
    assert workflow["description"]
    assert "workflow" in workflow["tags"]


def test_web_search_uses_explicit_tools_without_filter_side_channels():
    data = _catalog()
    assert data["models"]["web-search"]["tool_ids"] == [
        "web_research",
        "request_user_clarification",
        "docling_ingestion",
    ]
    assert "agentic_react_loop" not in data["functions"]
    assert "document_ingestion_router" not in data["functions"]
    assert {"agentic_react_loop", "document_ingestion_router"} <= set(data["archive_functions"])
    prompt = data["models"]["web-search"]["params"]["system"]
    assert "Deep Web MCP owns multi-hop search" in prompt
    assert "Thought block" not in prompt
    assert "__clarification_request__" not in prompt


def test_clarification_tool_returns_user_facing_markdown_without_magic_signal():
    tool = _load_tool("request_user_clarification")
    result = tool.request_user_clarification("Which product should I compare?", "undefined_baseline")
    assert "Which product should I compare?" in result
    assert "undefined_baseline" in result
    assert "__clarification_request__" not in result
    assert not result.lstrip().startswith("{")


def test_clarification_tool_truncates_and_handles_empty_question():
    tool = _load_tool("request_user_clarification")
    assert len(tool.request_user_clarification("Q" * 2000)) < 700
    assert "Could you clarify" in tool.request_user_clarification("   ")


def test_web_research_thin_tool_delegates_policy_to_service():
    source = (TOOL_ROOT / "web_research.py").read_text(encoding="utf-8")
    tool = _load_tool("web_research")
    assert source.count("httpx.AsyncClient") == 1
    assert "deep-web-mcp:8000/research" in source
    assert not hasattr(tool.valves, "max_hops")
    assert not hasattr(tool.valves, "total_budget_seconds")
    assert not hasattr(tool.valves, "per_hop_timeout_seconds")


def test_web_research_policy_is_centralized():
    policy = (ROOT / "deep-web-mcp" / "policy.py").read_text(encoding="utf-8")
    research = (ROOT / "deep-web-mcp" / "research.py").read_text(encoding="utf-8")
    mcp_tools = (ROOT / "deep-web-mcp" / "mcp_tools.py").read_text(encoding="utf-8")
    for name in (
        "RESEARCH_MAX_HOPS",
        "RESEARCH_TOTAL_BUDGET_S",
        "RESEARCH_MAX_SOURCES",
        "RESEARCH_MAX_ITERATIONS",
    ):
        assert name in policy
        assert name in research or name == "RESEARCH_MAX_HOPS"
        assert name in mcp_tools


def test_web_research_has_sufficiency_gate_and_trace():
    source = (ROOT / "deep-web-mcp" / "research.py").read_text(encoding="utf-8")
    assert "_is_sufficient" in source
    assert "hop_trace" in source
    assert "ceiling_hit" in source
    assert "{year}" in source


def test_web_research_is_sufficient_gate_logic():
    import sys

    if str(ROOT / "deep-web-mcp") not in sys.path:
        sys.path.insert(0, str(ROOT / "deep-web-mcp"))
    path = ROOT / "deep-web-mcp" / "research.py"
    spec = importlib.util.spec_from_file_location("research", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._is_sufficient([]) is False
    assert module._is_sufficient([{"verified": True, "summary": "A" * 500}]) is False
    assert module._is_sufficient(
        [{"verified": True, "summary": "A" * 250}, {"verified": True, "summary": "B" * 250}]
    ) is True


@pytest.mark.asyncio
async def test_web_research_stops_when_budget_is_exhausted():
    import asyncio
    import sys

    if str(ROOT / "deep-web-mcp") not in sys.path:
        sys.path.insert(0, str(ROOT / "deep-web-mcp"))
    path = ROOT / "deep-web-mcp" / "research.py"
    spec = importlib.util.spec_from_file_location("research_budget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def mock_search(query, filters, limit):
        await asyncio.sleep(0.05)
        return []

    module._search = mock_search
    result = await module.research_web(query="test", max_hops=4, total_budget_s=2.02)
    assert result["status"] == "success"
    assert len(result["hop_trace"]) == 1


def test_workflow_tools_are_cpu_only():
    _cpu_only_check(TOOL_ROOT / "web_research.py")
    _cpu_only_check(TOOL_ROOT / "request_user_clarification.py")
