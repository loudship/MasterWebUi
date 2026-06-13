import copy
import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FUNCTION_ROOT = ROOT / "workspace" / "catalog-functions"


def load_function(name):
    path = FUNCTION_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Filter()


def test_router_attachment_precedence_and_text_routing():
    router = load_function("dynamic_intent_router")
    body = {"model": "qwen35", "files": [{"id": "x"}], "messages": [{"role": "user", "content": "write code"}]}
    assert router.inlet(body)["model"] == "qwen257b"
    assert router.inlet({"model": "qwen35", "messages": [{"role": "user", "content": "analyze data"}]})["model"] == "-data-analyst--developer"
    assert router.inlet({"model": "qwen35", "messages": [{"role": "user", "content": "hello"}]})["model"] == "qwen35"


def test_router_detects_multimodal_and_preserves_unrelated_fields():
    router = load_function("dynamic_intent_router")
    body = {
        "model": "qwen35",
        "custom": {"keep": True},
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}],
    }
    assert router.inlet(body)["model"] == "qwen257b"
    assert body["custom"] == {"keep": True}


def test_context_injector_handles_multiple_triggers_once():
    injector = load_function("local_project_context_injector")
    body = {"messages": [{"role": "user", "content": "Compare Synthetic Sunrise with Jarvis."}]}
    result = injector.inlet(copy.deepcopy(body))
    assert result["messages"][0]["role"] == "system"
    assert "Synthetic Sunrise:" in result["messages"][0]["content"]
    assert "Jarvis:" in result["messages"][0]["content"]
    assert injector.inlet(result)["messages"] == result["messages"]


def test_context_injector_ignores_unmatched_prompt():
    injector = load_function("local_project_context_injector")
    body = {"messages": [{"role": "user", "content": "Ordinary request"}]}
    assert injector.inlet(copy.deepcopy(body)) == body


def test_formatter_marks_artifacts_and_is_idempotent():
    formatter = load_function("brutalist_artifact_formatter")
    content = "```python\nprint('x')\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n> quote\n"
    body = {"id": "a", "messages": [{"id": "a", "role": "assistant", "content": content}]}
    result = formatter.outlet(body)
    marked = result["messages"][0]["content"]
    assert marked.count("brutalist-artifact-marker") == 3
    assert formatter.outlet(result)["messages"][0]["content"] == marked


def test_formatter_targets_only_selected_assistant_message():
    formatter = load_function("brutalist_artifact_formatter")
    body = {
        "id": "target",
        "messages": [
            {"id": "old", "role": "assistant", "content": "> old"},
            {"id": "target", "role": "assistant", "content": "> new"},
        ],
    }
    result = formatter.outlet(body)
    assert "brutalist-artifact-marker" not in result["messages"][0]["content"]
    assert "brutalist-artifact-marker" in result["messages"][1]["content"]


def test_function_sources_are_cpu_only():
    forbidden = {"torch", "cuda", "subprocess", "requests", "aiohttp", "httpx", "socket"}
    for path in FUNCTION_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert imports.isdisjoint(forbidden)
