"""
tests/test_react_agentic_loop.py
=================================
Regression and new-feature tests for the ReAct Agentic Evaluation Loop.

Coverage
--------
1.  Catalog baseline: web-search model has both tools wired + full ReAct prompt
2.  Catalog baseline: agentic_react_loop function is registered
3.  System prompt structural assertions (Thought / Action / Observation / Evaluation)
4.  System prompt semantic tool-binding rules (no keyword autonomy)
5.  System prompt 4-hop ceiling declaration
6.  web_research tool: MAX_HOPS constant = 4
7.  web_research tool: asyncio Semaphore concurrency wiring
8.  web_research tool: _is_sufficient gate logic
9.  web_research tool: hop_trace in report
10. web_research tool: ceiling_hit flag in output
11. web_research tool: CPU-only import compliance
12. request_user_clarification tool: JSON signal contract
13. request_user_clarification tool: question truncation
14. request_user_clarification tool: CPU-only import compliance
15. agentic_react_loop filter: outlet intercepts clarification signal
16. agentic_react_loop filter: outlet leaves normal messages unchanged
17. agentic_react_loop filter: inlet injects re-entry guard after clarification
18. agentic_react_loop filter: idempotency (double-outlet pass)
19. agentic_react_loop filter: priority valve is 50
20. agentic_react_loop filter: CPU-only import compliance
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FUNCTION_ROOT = ROOT / "workspace" / "catalog-functions"
TOOL_ROOT = ROOT / "workspace" / "catalog-tools"
CATALOG = ROOT / "workspace" / "catalog-baseline.yaml"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_tool(name: str):
    path = TOOL_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Tools()


def _load_function(name: str):
    path = FUNCTION_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Filter()


def _load_function_module(name: str):
    path = FUNCTION_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog_text() -> str:
    return CATALOG.read_text(encoding="utf-8")


def _web_research_text() -> str:
    return (TOOL_ROOT / "web_research.py").read_text(encoding="utf-8")


def _clarification_text() -> str:
    return (TOOL_ROOT / "request_user_clarification.py").read_text(encoding="utf-8")


def _react_filter_text() -> str:
    return (FUNCTION_ROOT / "agentic_react_loop.py").read_text(encoding="utf-8")


def _cpu_only_check(path: Path) -> None:
    """Assert no GPU/network/subprocess imports are used."""
    forbidden = {"torch", "cuda", "subprocess", "requests", "aiohttp", "socket"}
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
# 1-5: Catalog baseline assertions
# ===========================================================================

def test_catalog_web_search_model_has_both_tool_ids():
    import json as _json
    data = _json.loads(_catalog_text())
    tool_ids = data["models"]["web-search"]["tool_ids"]
    assert "web_research" in tool_ids
    assert "request_user_clarification" in tool_ids
    assert "docling_ingestion" in tool_ids


def test_catalog_agentic_react_loop_function_is_registered():
    import json as _json
    data = _json.loads(_catalog_text())
    functions = data["functions"]
    assert "agentic_react_loop" in functions
    assert functions["agentic_react_loop"]["active"] is True
    assert functions["agentic_react_loop"]["global"] is False
    assert "web-search" in functions["agentic_react_loop"]["model_ids"]


def test_catalog_request_user_clarification_tool_is_registered():
    import json as _json
    data = _json.loads(_catalog_text())
    assert "request_user_clarification" in data["tools"]


def test_catalog_web_search_system_prompt_contains_react_cycle():
    import json as _json
    data = _json.loads(_catalog_text())
    prompt = data["models"]["web-search"]["params"]["system"]
    assert "**Thought**" in prompt
    assert "**Action**" in prompt
    assert "**Observation**" in prompt
    assert "**Evaluation**" in prompt


def test_catalog_web_search_system_prompt_mandates_thought_before_every_invocation():
    import json as _json
    data = _json.loads(_catalog_text())
    prompt = data["models"]["web-search"]["params"]["system"]
    assert "Thought block before every single tool invocation" in prompt


def test_catalog_web_search_system_prompt_declares_4_hop_ceiling():
    import json as _json
    data = _json.loads(_catalog_text())
    prompt = data["models"]["web-search"]["params"]["system"]
    assert "4" in prompt
    assert "ceiling" in prompt.lower() or "4-hop" in prompt.lower()


def test_catalog_web_search_system_prompt_forbids_keyword_autonomy():
    import json as _json
    data = _json.loads(_catalog_text())
    prompt = data["models"]["web-search"]["params"]["system"]
    # Must explicitly state that tool selection is NOT based on keyword presence
    assert "structural data requirements" in prompt
    assert "keyword" in prompt.lower()


def test_catalog_web_search_system_prompt_mandates_verified_markdown_links():
    import json as _json
    data = _json.loads(_catalog_text())
    prompt = data["models"]["web-search"]["params"]["system"]
    assert "Markdown" in prompt
    assert "verified" in prompt.lower()
    assert "clickable" in prompt.lower()


# ===========================================================================
# 6-11: web_research tool
# ===========================================================================

def test_web_research_declares_max_hops_4():
    src = _web_research_text()
    assert "MAX_HOPS: int = 4" in src or "MAX_HOPS = 4" in src


def test_web_research_has_no_direct_egress_path():
    """Link validation must happen inside deep-web-mcp (the controlled egress
    perimeter). Direct probing from the Open WebUI container bypassed
    ALLOWED_TARGET_HOSTS and burned 8 s per guaranteed-failed probe on the
    masquerade-disabled network (audit P2-5)."""
    src = _web_research_text()
    assert "_validate_link" not in src
    assert "follow_redirects" not in src
    # The only outbound call surface is the deep-web-mcp /research endpoint.
    assert src.count("httpx.AsyncClient") == 1
    assert "deep-web-mcp:8000/research" in src


def test_web_research_declares_time_budgets():
    src = _web_research_text()
    assert "PER_HOP_TIMEOUT_S: int = 30" in src or "PER_HOP_TIMEOUT_S = 30" in src
    assert "TOTAL_BUDGET_S: int = 90" in src or "TOTAL_BUDGET_S = 90" in src
    assert "budget_exhausted" in src


def test_web_research_has_sufficiency_gate():
    src = _web_research_text()
    assert "_is_sufficient" in src
    assert "MIN_ACTIVE_SOURCES_SUFFICIENT" in src
    assert "MIN_EVIDENCE_CHARS_SUFFICIENT" in src


def test_web_research_is_sufficient_gate_logic():
    """Unit-test the _is_sufficient gate with mock source data."""
    tool = _load_tool("web_research")
    cls = type(tool)

    # Too few active sources
    sources_empty: list = []
    assert cls._is_sufficient(sources_empty) is False

    # Only 1 active source — below threshold
    sources_one = [{"verified": True, "summary": "A" * 500}]
    assert cls._is_sufficient(sources_one) is False

    # 2 active sources, enough evidence
    sources_ok = [
        {"verified": True, "summary": "A" * 250},
        {"verified": True, "summary": "B" * 250},
    ]
    assert cls._is_sufficient(sources_ok) is True

    # 2 active but tiny evidence — insufficient
    sources_small = [
        {"verified": True, "summary": "A"},
        {"verified": True, "summary": "B"},
    ]
    assert cls._is_sufficient(sources_small) is False


def test_web_research_hop_trace_in_report():
    src = _web_research_text()
    assert "hop_trace" in src
    assert "ceiling_hit" in src


def test_web_research_gap_pattern_templates_declared():
    src = _web_research_text()
    assert "_GAP_PATTERNS" in src
    assert "{query}" in src


def test_web_research_cpu_only_imports():
    _cpu_only_check(TOOL_ROOT / "web_research.py")


def test_web_research_tool_valves_max_hops_default_4():
    tool = _load_tool("web_research")
    assert tool.valves.max_hops == 4


def test_web_research_tool_valves_time_budgets():
    tool = _load_tool("web_research")
    assert tool.valves.timeout_seconds == 30
    assert tool.valves.total_budget_seconds == 90
    assert not hasattr(tool.valves, "link_semaphore")


@pytest.mark.asyncio
async def test_web_research_caps_hop_timeout_to_remaining_budget():
    """Each /research call must be bounded by min(per-hop, remaining budget)."""
    import json as _json

    tool = _load_tool("web_research")
    tool.valves.total_budget_seconds = 10
    seen_timeouts = []

    async def fake_call(query, strategy, domain_filters, max_iterations, max_sources, timeout_s):
        seen_timeouts.append(timeout_s)
        return {"status": "success", "strategy": strategy, "sources": []}

    tool._call_research = fake_call
    result = _json.loads(await tool.research_web("local llm quantization"))

    assert result["status"] == "success"
    assert result["ceiling_hit"] is True          # never sufficient → 4 hops
    assert len(seen_timeouts) == 4
    assert all(timeout <= 10 for timeout in seen_timeouts)


def test_web_research_gap_patterns_use_dynamic_year():
    src = _web_research_text()
    assert "{year}" in src
    assert "2024 2025" not in src, "hardcoded years go stale"


# ===========================================================================
# 12-14: request_user_clarification tool
# ===========================================================================

def test_clarification_tool_emits_signal_contract():
    tool = _load_tool("request_user_clarification")
    result = tool.request_user_clarification(
        question="What specific product are you comparing?",
        ambiguity_type="undefined_baseline",
    )
    data = json.loads(result)
    assert data["__clarification_request__"] is True
    assert data["question"] == "What specific product are you comparing?"
    assert data["ambiguity_type"] == "undefined_baseline"


def test_clarification_tool_truncates_long_question():
    tool = _load_tool("request_user_clarification")
    long_q = "Q" * 2000
    result = tool.request_user_clarification(question=long_q)
    data = json.loads(result)
    assert len(data["question"]) <= tool.valves.max_question_chars


def test_clarification_tool_handles_empty_question():
    tool = _load_tool("request_user_clarification")
    result = tool.request_user_clarification(question="   ")
    data = json.loads(result)
    assert data["__clarification_request__"] is True
    assert len(data["question"]) > 0  # fallback question provided


def test_clarification_tool_cpu_only_imports():
    _cpu_only_check(TOOL_ROOT / "request_user_clarification.py")


# ===========================================================================
# 15-20: agentic_react_loop filter
# ===========================================================================

def test_react_filter_outlet_intercepts_clarification_signal():
    filt = _load_function("agentic_react_loop")
    signal = json.dumps({"__clarification_request__": True, "question": "Which AI model?", "ambiguity_type": "missing_scope"})
    body = {
        "id": "msg1",
        "messages": [{"id": "msg1", "role": "assistant", "content": signal}],
    }
    result = filt.outlet(body)
    content = result["messages"][0]["content"]
    assert "__clarification_request__" not in content
    assert "Which AI model?" in content
    assert "[agentic-clarification-pending:v1]" in content


def test_react_filter_outlet_leaves_normal_message_unchanged():
    filt = _load_function("agentic_react_loop")
    normal_content = "Here are your research results:\n\n1. [Example](https://example.com)"
    body = {
        "id": "msg1",
        "messages": [{"id": "msg1", "role": "assistant", "content": normal_content}],
    }
    result = filt.outlet(body)
    assert result["messages"][0]["content"] == normal_content


def test_react_filter_outlet_is_idempotent():
    filt = _load_function("agentic_react_loop")
    signal = json.dumps({"__clarification_request__": True, "question": "Clarify scope?", "ambiguity_type": "missing_scope"})
    body = {
        "id": "m1",
        "messages": [{"id": "m1", "role": "assistant", "content": signal}],
    }
    pass1 = filt.outlet(body)
    pass2 = filt.outlet(copy.deepcopy(pass1))
    # Second pass should not double-wrap
    assert pass1["messages"][0]["content"] == pass2["messages"][0]["content"]


def test_react_filter_inlet_injects_reentry_after_clarification():
    filt = _load_function("agentic_react_loop")
    # Body that has a prior clarification marker in an assistant message
    body = {
        "messages": [
            {"role": "user", "content": "Compare X and Y"},
            {"role": "assistant", "content": "[agentic-clarification-pending:v1]\n\n**Before I search**..."},
            {"role": "user", "content": "I mean GPT-4 and Claude 3.5"},
        ]
    }
    result = filt.inlet(body)
    assert result["messages"][0]["role"] == "system"
    assert "agentic-clarification-pending:v1" in result["messages"][0]["content"]
    assert "re-enter" in result["messages"][0]["content"].lower() or "Re-enter" in result["messages"][0]["content"]


def test_react_filter_inlet_does_not_inject_without_prior_clarification():
    filt = _load_function("agentic_react_loop")
    body = {
        "messages": [
            {"role": "user", "content": "What is quantum computing?"},
        ]
    }
    original_len = len(body["messages"])
    result = filt.inlet(body)
    assert len(result["messages"]) == original_len


def test_react_filter_priority_is_50():
    filt = _load_function("agentic_react_loop")
    assert filt.valves.priority == 50


def test_react_filter_cpu_only_imports():
    _cpu_only_check(FUNCTION_ROOT / "agentic_react_loop.py")


def test_react_filter_handles_malformed_json_gracefully():
    filt = _load_function("agentic_react_loop")
    body = {
        "id": "m1",
        "messages": [{"id": "m1", "role": "assistant", "content": '{"__clarification_request__": invalid_json}'}],
    }
    result = filt.outlet(body)
    # Should not crash and should leave content unchanged (no valid signal)
    assert result is not None


def test_react_filter_outlet_embeds_ambiguity_type():
    filt = _load_function("agentic_react_loop")
    signal = json.dumps(
        {"__clarification_request__": True, "question": "What domain?", "ambiguity_type": "missing_scope"}
    )
    body = {"id": "x", "messages": [{"id": "x", "role": "assistant", "content": signal}]}
    result = filt.outlet(body)
    content = result["messages"][0]["content"]
    assert "missing_scope" in content


# ===========================================================================
# Back-compat: existing deep MCP contract still holds after our changes
# ===========================================================================

def test_web_research_tool_source_file_exists():
    assert (TOOL_ROOT / "web_research.py").is_file()


def test_request_clarification_tool_source_file_exists():
    assert (TOOL_ROOT / "request_user_clarification.py").is_file()


def test_agentic_react_loop_filter_source_file_exists():
    assert (FUNCTION_ROOT / "agentic_react_loop.py").is_file()


def test_existing_three_global_filters_are_still_registered():
    import json as _json
    data = _json.loads(_catalog_text())
    fns = data["functions"]
    assert fns["dynamic_intent_router"]["global"] is True
    assert fns["local_project_context_injector"]["global"] is True
    # brutalist formatter is NOT global (per original config) but still present
    assert "brutalist_artifact_formatter" in fns


def test_agentic_filter_is_not_global_filter():
    """Confirm the new filter is scoped to web-search only — does not pollute global chain."""
    import json as _json
    data = _json.loads(_catalog_text())
    filt = data["functions"]["agentic_react_loop"]
    assert filt["global"] is False
    assert "web-search" in filt["model_ids"]
