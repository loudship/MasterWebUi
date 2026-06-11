from pathlib import Path

import pytest

import monitor_daemon as monitor


def reset_monitor_state():
    monitor._eval_history.clear()
    monitor._active_operations.clear()
    monitor._recent_operations.clear()


def test_operation_tracking_exposes_pipeline_progress_and_completion():
    reset_monitor_state()
    monitor._start_operation("eval-1", "https://example.test")
    monitor._set_operation_step("eval-1", "extraction", "Fetching content")
    monitor._set_operation_step("eval-1", "embedding", "Embedding content")

    active = monitor._active_operations["eval-1"]
    assert active.current_step == "embedding"
    assert active.steps[0].status == "success"
    assert active.steps[1].status == "running"

    monitor._record_eval(
        monitor.EvalRecord(
            eval_id="eval-1",
            url="https://example.test",
            timestamp=1.0,
            outcome="unchanged",
            distance=0.01,
        )
    )

    assert not monitor._active_operations
    assert monitor._recent_operations[0].status == "success"
    assert monitor._recent_operations[0].steps[1].status == "success"
    assert monitor._recent_operations[0].steps[2].status == "skipped"


@pytest.mark.asyncio
async def test_overview_reports_backend_and_error_summary(monkeypatch):
    reset_monitor_state()
    monitor._record_eval(
        monitor.EvalRecord(
            eval_id="eval-error",
            url="https://example.test",
            timestamp=1.0,
            outcome="error",
            error_code="EXTRACTION_FAILURE",
        )
    )

    async def fake_connectivity():
        return [
            {
                "name": "qdrant",
                "label": "Qdrant",
                "status": "offline",
                "detail": "connection refused",
                "latency_ms": 2,
            }
        ]

    monkeypatch.setattr(monitor, "_backend_connectivity", fake_connectivity)
    overview = await monitor.operations_overview()

    assert overview["summary"]["system_state"] == "Attention needed"
    assert overview["summary"]["error_rate_percent"] == 100.0
    assert overview["history"][0]["error_code"] == "EXTRACTION_FAILURE"


@pytest.mark.asyncio
async def test_dashboard_asset_is_available():
    response = await monitor.dashboard()
    assert Path(response.path).name == "index.html"
    assert Path(response.path).exists()
    dashboard = Path(response.path).read_text(encoding="utf-8")
    assert "Web Tools Control Center" in dashboard
    assert 'data-view="validation"' in dashboard
    assert 'data-view="prompt"' in dashboard
    assert 'data-view="history"' in dashboard


@pytest.mark.asyncio
async def test_web_tools_overview_reports_connected_tools(monkeypatch):
    async def fake_connectivity():
        return [
            {"name": "searxng", "status": "reachable"},
            {"name": "crawl4ai", "status": "offline"},
        ]

    monkeypatch.setattr(monitor, "_backend_connectivity", fake_connectivity)
    overview = await monitor.web_tools_overview()

    assert overview["summary"]["connected"] == 1
    assert overview["summary"]["total"] == 2
    assert overview["summary"]["health_percent"] == 50


@pytest.mark.asyncio
async def test_crawl_and_firecrawl_fallback_use_local_compatibility_bridge(monkeypatch):
    async def fake_scrape(url, timeout_seconds):
        return {"mode": "crawl4ai", "url": url, "content": "example", "metadata": {}}

    monkeypatch.setattr(monitor, "_crawl4ai_compatibility_scrape", fake_scrape)
    monkeypatch.setattr(monitor, "FIRECRAWL_URL", "")
    request = monitor.ScrapeRequest(url="https://example.com", timeout_seconds=15)

    crawl = await monitor.web_tools_crawl(request)
    firecrawl = await monitor.web_tools_firecrawl(request)

    assert crawl["mode"] == "crawl4ai"
    assert firecrawl["mode"] == "crawl4ai_compatibility"
    assert crawl["content"] == firecrawl["content"] == "example"


def test_scrape_rejects_non_web_urls():
    with pytest.raises(monitor.HTTPException) as exc:
        monitor._require_web_url("file:///etc/passwd")
    assert exc.value.status_code == 422
