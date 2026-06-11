from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "deep-web-mcp" / "server.py").read_text(encoding="utf-8")


def test_deep_web_mcp_preserves_open_webui_bridge_contract():
    assert "async def fetch_deep_web_data(" in SERVER
    assert "url:              str" in SERVER
    assert "js_script:        str  = None" in SERVER
    assert "async def search_deep_web_database(" in SERVER
    assert "async def discover_web_layouts(" in SERVER


def test_deep_web_mcp_exposes_real_health_search_and_extract_routes():
    assert '@app.get("/health")' in SERVER
    assert '@app.get("/health/validation")' in SERVER
    assert '@app.post("/search")' in SERVER
    assert '@app.post("/discover")' in SERVER
    assert '@app.post("/extract/stream")' in SERVER
    assert 'app.mount("/", mcp.sse_app())' in SERVER
    assert 'MCP_URL}/health' in (ROOT / "monitor_daemon.py").read_text(encoding="utf-8")


def test_public_target_mode_is_guarded_and_onion_is_rejected():
    assert 'ALLOW_PUBLIC_TARGETS' in SERVER
    assert 'if not ip.is_global' in SERVER
    assert '".onion targets are not supported by this direct-extraction service."' in SERVER


def test_current_crawl4ai_markdown_contract_is_used():
    assert 'markdown_result = getattr(crawler_result, "markdown", None)' in SERVER
    assert "crawler_result.markdown_v2" not in SERVER


def test_search_contract_is_compact_and_exposes_ranked_best_match():
    assert '"best_match": results[0] if results else None' in SERVER
    assert "results = results[:5]" in SERVER


def test_discovery_contract_requires_json_only_items():
    assert "discover_web_layouts" in SERVER
    assert "@app.post(\"/discover\")" in SERVER
