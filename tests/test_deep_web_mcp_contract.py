from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "deep-web-mcp" / "server.py").read_text(encoding="utf-8")
API = (ROOT / "deep-web-mcp" / "api.py").read_text(encoding="utf-8")
MCP = (ROOT / "deep-web-mcp" / "mcp_tools.py").read_text(encoding="utf-8")
EXTRACTION = (ROOT / "deep-web-mcp" / "extraction.py").read_text(encoding="utf-8")
DISCOVERY = (ROOT / "deep-web-mcp" / "web_discovery.py").read_text(encoding="utf-8")


def test_deep_web_mcp_preserves_open_webui_bridge_contract():
    assert "async def fetch_deep_web_data(" in MCP
    assert "url: str" in MCP
    assert "js_script: str = None" in MCP
    assert "async def search_deep_web_database(" in MCP
    assert "async def discover_web_layouts(" in MCP
    assert "async def research_web(" in MCP


def test_deep_web_mcp_exposes_real_health_search_and_extract_routes():
    assert '@app.get("/health")' in API
    assert '@app.get("/health/validation")' in API
    assert '@app.post("/search")' in API
    assert '@app.post("/discover")' in API
    assert '@app.post("/research")' in API
    assert '@app.post("/extract/stream")' in API
    assert 'app.mount("/", mcp.sse_app())' in API
    assert 'MCP_URL}/health' in (ROOT / "monitor_daemon.py").read_text(encoding="utf-8")


def test_public_target_mode_is_guarded_and_onion_is_rejected():
    assert 'ALLOW_PUBLIC_TARGETS' in EXTRACTION
    assert 'if not ip.is_global' in EXTRACTION
    assert '".onion targets are not supported by this direct-extraction service."' in EXTRACTION


def test_current_crawl4ai_markdown_contract_is_used():
    assert 'markdown_result = getattr(crawler_result, "markdown", None)' in EXTRACTION
    assert "crawler_result.markdown_v2" not in EXTRACTION


def test_search_contract_is_compact_and_exposes_ranked_best_match():
    assert '"best_match":          results[0] if results else None' in MCP
    assert "results = results[:5]" in MCP


def test_discovery_contract_requires_json_only_items():
    assert "discover_web_layouts" in MCP
    assert "@app.post(\"/discover\")" in API


def test_research_contract_is_bounded_and_exposes_verified_links():
    research = (ROOT / "deep-web-mcp" / "research.py").read_text(encoding="utf-8")
    assert 'Literal["auto", "general", "deep"]' in research
    assert "RESEARCH_MAX_ITERATIONS" in research
    assert "RESEARCH_MAX_SOURCES" in research
    assert '"verified_url"' in research
    assert '"markdown_report"' in research
