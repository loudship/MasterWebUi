import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reconcile_workspace_catalog.py"


def load_reconciler():
    spec = importlib.util.spec_from_file_location("workspace_reconciler", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_baseline_has_exact_target_catalog():
    baseline = json.loads((ROOT / "workspace" / "catalog-baseline.yaml").read_text(encoding="utf-8"))
    assert set(baseline["models"]) == {"qwen35", "qwen257b", "moyclark", "-data-analyst--developer", "web-search"}
    # 12 tools: original 10 + request_user_clarification + docling_ingestion
    assert len(baseline["tools"]) == 12
    assert "request_user_clarification" in baseline["tools"]
    assert "docling_ingestion" in baseline["tools"]
    # 5 functions: original 3 + agentic_react_loop + document_ingestion_router
    assert set(baseline["functions"]) == {
        "dynamic_intent_router",
        "local_project_context_injector",
        "brutalist_artifact_formatter",
        "agentic_react_loop",
        "document_ingestion_router",
    }
    assert baseline["archive_tools"] == []
    assert set(baseline["archive_functions"]) == {"comfy_mcp_pipeline", "langfuse_filter"}
    assert baseline["models"]["qwen35"]["builtin_tools"] == ["web_search", "code_interpreter"]
    assert baseline["models"]["-data-analyst--developer"]["builtin_tools"] == ["code_interpreter"]
    assert baseline["models"]["moyclark"]["knowledge_context"] == "full"
    # web-search now has three tool IDs: base research + clarification intercept + docling ingestion
    assert "web_research" in baseline["models"]["web-search"]["tool_ids"]
    assert "request_user_clarification" in baseline["models"]["web-search"]["tool_ids"]
    assert "docling_ingestion" in baseline["models"]["web-search"]["tool_ids"]
    assert baseline["functions"]["dynamic_intent_router"]["global"] is True
    assert baseline["functions"]["local_project_context_injector"]["global"] is True
    assert baseline["functions"]["brutalist_artifact_formatter"]["global"] is False
    # agentic_react_loop is model-scoped (not global) to avoid polluting the global filter chain
    assert baseline["functions"]["agentic_react_loop"]["global"] is False
    # document_ingestion_router is also model-scoped at priority 40
    assert baseline["functions"]["document_ingestion_router"]["global"] is False
    assert baseline["functions"]["document_ingestion_router"]["priority"] == 40


def test_desired_models_use_a_narrow_builtin_tool_allowlist():
    reconciler = load_reconciler()
    baseline = reconciler.load_json_yaml(reconciler.DEFAULT_BASELINE)
    current = {
        "id": "qwen35",
        "name": "qwen35",
        "base_model_id": "base",
        "meta": {"capabilities": {"builtin_tools": False}},
        "params": {},
        "access_grants": [],
        "is_active": True,
    }
    result = reconciler.desired_model(current, baseline["models"]["qwen35"], baseline["knowledge"])
    assert result["meta"]["capabilities"]["builtin_tools"] is True
    assert result["meta"]["builtinTools"]["notes"] is False
    assert result["meta"]["builtinTools"]["calendar"] is False
    assert "web_search" not in result["meta"]["builtinTools"]
    assert "code_interpreter" not in result["meta"]["builtinTools"]


def test_dry_run_detects_model_metadata_drift():
    reconciler = load_reconciler()
    baseline = reconciler.load_json_yaml(reconciler.DEFAULT_BASELINE)
    current = {
        "id": "qwen35",
        "name": baseline["models"]["qwen35"]["name"],
        "base_model_id": "base",
        "meta": {"capabilities": {"builtin_tools": False}},
        "params": {},
        "access_grants": [],
        "is_active": True,
    }
    snapshot = {
        "models": [current],
        "tools": [],
        "functions": [],
        "skills": [],
        "prompts": [],
        "knowledge": [],
    }
    actions = reconciler.expected_actions(snapshot, baseline)
    assert "update model qwen35" in actions
    assert "reconcile knowledge document galadriel-roleplay-profile.md" in actions


def test_sanitizer_redacts_credentials_and_secret_literals():
    reconciler = load_reconciler()
    result = reconciler.sanitize({"api_key": "abc", "content": "token sk-supersecret123456789"})
    assert result["api_key"] == "[REDACTED]"
    assert "supersecret" not in result["content"]


def test_sandbox_patch_disables_network_install_and_updates():
    reconciler = load_reconciler()
    source = "\n".join(
        f"{field}: bool = pydantic.Field(\n            default=True,"
        for field in ("NETWORKING_ALLOWED", "AUTO_INSTALL", "CHECK_FOR_UPDATES")
    )
    patched = reconciler.patch_sandbox(source)
    assert patched.count("default=False") == 3
    assert "default=True" not in patched
    assert reconciler.patch_sandbox(patched) == patched


def test_inline_visualizer_patch_removes_public_script_cdns():
    reconciler = load_reconciler()
    source = '_KNOWN_CDNS = (\n    "https://cdnjs.cloudflare.com" " https://cdn.jsdelivr.net" " https://unpkg.com"\n)\n'
    patched = reconciler.patch_inline_visualizer_local_only(source)
    assert patched == '_KNOWN_CDNS = ""\n'
    assert reconciler.patch_inline_visualizer_local_only(patched) == patched


def test_method_removal_strips_state_changing_wrapper_actions():
    reconciler = load_reconciler()
    source = "class Tools:\n    def read(self):\n        return 1\n\n    async def create_event(self):\n        return 2\n\n    def tail(self):\n        return 3\n"
    patched = reconciler.remove_python_method(source, "create_event")
    assert "create_event" not in patched
    assert "def read" in patched
    assert "def tail" in patched
    assert reconciler.remove_python_method(patched, "create_event") == patched


def test_replacement_tools_are_read_only_or_confirmation_gated():
    swarm = (ROOT / "workspace" / "catalog-tools" / "orchestrator_status.py").read_text(encoding="utf-8")
    deep = (ROOT / "workspace" / "catalog-tools" / "deep_web_readonly.py").read_text(encoding="utf-8")
    advanced = (ROOT / "workspace" / "catalog-tools" / "deep_web_advanced.py").read_text(encoding="utf-8")
    calendar = (ROOT / "workspace" / "catalog-tools" / "calendar_readonly.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "start_orchestrator", "evict_vram", "invoke_swarm"):
        assert forbidden not in swarm
    assert '"session_required": False' in deep
    assert '"js_script": ""' in deep
    assert "CONFIRM_ADVANCED_DEEP_WEB" in advanced
    assert "streamablehttp_client" in calendar
    assert "sse_client" not in calendar
    assert "create_event" not in calendar


def test_ui_override_contracts_are_present():
    override = ROOT / "workspace" / "open-webui-overrides"
    patch = (override / "patch_frontend.mjs").read_text(encoding="utf-8")
    router = (override / "backend" / "open_webui" / "routers" / "workspace_catalog.py").read_text(encoding="utf-8")
    assert "CatalogFilters" in patch
    assert "CatalogBadges" in patch
    assert '@router.get("/status"' in router
    assert 'kind="function"' in router
    assert "/workspace/functions" in patch
