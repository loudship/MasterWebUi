import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deep-web-mcp" / "web_discovery.py"


def load_module():
    spec = importlib.util.spec_from_file_location("web_discovery", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_domain_filters_are_normalized_and_apply_to_hosts():
    module = load_module()
    filters = module._normalize_filters(
        [
            {"domain": "Example.com", "mode": "include"},
            {"domain": "ads.example.com", "mode": "exclude"},
        ]
    )
    assert module._allowed_by_filters("https://docs.example.com/page", filters) is True
    assert module._allowed_by_filters("https://ads.example.com/page", filters) is False


def test_truncation_warning_is_appended_when_budget_is_exceeded():
    module = load_module()
    layout, truncated = module._truncate_layout(
        "alpha beta gamma delta epsilon",
        max_tokens=3,
        max_chars=100,
    )
    assert truncated is True
    assert module.TRUNCATION_WARNING_TOKEN in layout


@pytest.mark.asyncio
async def test_discovery_returns_json_items_and_skips_failed_extractions(monkeypatch):
    module = load_module()

    async def fake_get_json(_url, params=None):
        return {
            "results": [
                {"url": "https://docs.example.com/a", "title": "Alpha"},
                {"url": "https://blocked.example.com/b", "title": "Blocked"},
            ]
        }

    async def fake_extract(url, max_tokens, max_chars):
        if "blocked" in url:
            return {
                "status": "error",
                "error_code": "ANTI_BOT_DETECTED",
                "reason": "blocked",
                "url": url,
            }
        return {
            "status": "success",
            "canonical_heading": "Alpha heading",
            "layout": "Alpha layout",
            "truncated": False,
            "url": url,
        }

    monkeypatch.setattr(module, "_get_json", fake_get_json)
    monkeypatch.setattr(module, "extract_layout_document", fake_extract)
    monkeypatch.setattr(module, "_allowed_by_filters", lambda url, filters: "blocked" not in url)

    result = await module.discover_web_layouts(
        "alpha",
        domain_filters=[{"domain": "example.com", "mode": "include"}],
        max_tokens=100,
        max_chars=500,
        max_results=5,
    )

    assert result == [
        {
            "uri": "https://docs.example.com/a",
            "canonical_heading": "Alpha heading",
            "layout": "Alpha layout",
        }
    ]
