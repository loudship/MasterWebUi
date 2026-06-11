import json
import time

import pytest
import httpx

import monitor_daemon as monitor
from monitor_app.schemas import DiagnosticRunRequest, PromptVerifyRequest
from monitor_app.store import RunStore, run_markdown, sanitize_artifact


def test_store_persists_redacts_exports_and_prunes(tmp_path):
    store = RunStore(str(tmp_path / "runs.db"), retention_days=30, max_runs=2)
    for index in range(3):
        run_id = f"run-{index}"
        store.create_run(run_id, "test", {"authorization": "Bearer secret", "index": index})
        store.add_step(
            run_id,
            "probe",
            "Probe",
            "passed",
            "ok",
            {"api_key": "secret", "value": index},
            time.time(),
        )
        store.finish_run(run_id, "passed", {"passed": 1})

    runs = store.list_runs()
    assert len(runs) == 2
    run = store.get_run("run-2")
    assert run["request"]["authorization"] == "[REDACTED]"
    assert run["steps"][0]["evidence"]["api_key"] == "[REDACTED]"
    assert "# Web Tools Control Center Run run-2" in run_markdown(run)


def test_artifact_size_is_capped():
    result = sanitize_artifact({"body": "x" * 150_000})
    assert result["truncated"] is True
    assert len(result["preview"]) == 100_000


@pytest.mark.asyncio
async def test_safe_url_rejects_private_targets():
    with pytest.raises(monitor.HTTPException) as exc:
        await monitor._require_safe_public_url("http://127.0.0.1/private")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_probe_results_are_cached_and_force_refreshes(monkeypatch):
    calls = 0

    async def uncached():
        nonlocal calls
        calls += 1
        return [{"name": "service", "status": "reachable"}]

    monkeypatch.setattr(monitor, "_uncached_backend_connectivity", uncached)
    monkeypatch.setattr(monitor, "_probe_cache", None)
    await monitor._backend_connectivity()
    await monitor._backend_connectivity()
    assert calls == 1
    await monitor._backend_connectivity(force=True)
    assert calls == 2


@pytest.mark.asyncio
async def test_prompt_lab_is_visibly_unavailable_without_key(monkeypatch):
    monkeypatch.setattr(monitor, "OPEN_WEBUI_API_KEY", "")
    models = await monitor.prompt_models()
    result = await monitor.prompt_verify(PromptVerifyRequest(prompt="hello"))
    assert models["configured"] is False
    assert result.configured is False
    assert "OPEN_WEBUI_API_KEY" in result.detail


@pytest.mark.asyncio
async def test_diagnostic_suite_records_each_step(monkeypatch, tmp_path):
    store = RunStore(str(tmp_path / "runs.db"))
    monkeypatch.setattr(monitor, "_run_store", store)

    async def fake_connectivity(force=False):
        return [{"name": "all", "status": "reachable"}]

    async def fake_tool(_request):
        return {"passed": True, "content": "evidence"}

    async def fake_prompt(_request):
        return monitor.PromptVerifyResponse(
            configured=False, passed=False, model_id="qwen35", detail="not configured"
        )

    monkeypatch.setattr(monitor, "_backend_connectivity", fake_connectivity)
    monkeypatch.setattr(monitor, "web_tools_search", fake_tool)
    monkeypatch.setattr(monitor, "web_tools_deep_web_search", fake_tool)
    monkeypatch.setattr(monitor, "web_tools_deep_web_extract", fake_tool)
    monkeypatch.setattr(monitor, "web_tools_crawl", fake_tool)
    monkeypatch.setattr(monitor, "web_tools_firecrawl", fake_tool)
    monkeypatch.setattr(monitor, "_verify_prompt", fake_prompt)

    store.create_run("suite-1", "validation_suite", {})
    run = await monitor._run_diagnostic_suite("suite-1", DiagnosticRunRequest())
    assert run["status"] == "partial"
    assert run["summary"] == {"passed": 6, "failed": 0, "skipped": 1, "total": 7}
    assert len(run["steps"]) == 7


@pytest.mark.asyncio
async def test_report_endpoint_returns_typed_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor, "_run_store", RunStore(str(tmp_path / "runs.db")))
    response = await monitor.get_diagnostic_run("missing")
    payload = json.loads(response.body)
    assert response.status_code == 404
    assert payload["error"]["code"] == "run_not_found"


@pytest.mark.asyncio
async def test_successful_reports_export_markdown_and_json(monkeypatch, tmp_path):
    store = RunStore(str(tmp_path / "runs.db"))
    store.create_run("export-1", "validation_suite", {"query": "test"})
    store.add_step("export-1", "probe", "Probe", "passed", "ok", {"value": 1}, time.time())
    store.finish_run("export-1", "passed", {"passed": 1})
    monkeypatch.setattr(monitor, "_run_store", store)

    markdown = await monitor.diagnostic_run_report("export-1", "markdown")
    json_report = await monitor.diagnostic_run_report("export-1", "json")

    assert markdown.status_code == 200
    assert b"# Web Tools Control Center Run export-1" in markdown.body
    assert json_report.status_code == 200
    assert json.loads(json_report.body)["status"] == "passed"


@pytest.mark.asyncio
async def test_sse_endpoint_emits_update_event():
    class FakeRequest:
        async def is_disconnected(self):
            return False

    response = await monitor.control_events(FakeRequest())
    first = await response.body_iterator.__anext__()
    await response.body_iterator.aclose()

    assert response.media_type == "text/event-stream"
    assert "event: update" in first


@pytest.mark.asyncio
async def test_prompt_verification_checks_completion_citations_and_tools(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Evidence-backed response",
                            "tool_calls": [{"name": "search"}],
                        }
                    }
                ],
                "citations": [{"url": "https://example.com"}],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(monitor, "OPEN_WEBUI_API_KEY", "server-side-secret")
    monkeypatch.setattr(monitor.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    result = await monitor._verify_prompt(
        PromptVerifyRequest(
            prompt="test",
            expected_text=["evidence"],
            require_citations=True,
            require_tool_call=True,
        )
    )

    assert result.passed is True
    assert result.citations and result.tool_calls


def test_versioned_openapi_routes_have_typed_success_contracts():
    schema = monitor.app.openapi()
    typed_paths = [
        "/api/v1/control/overview",
        "/api/v1/control/probes/{service}",
        "/api/v1/diagnostics/runs",
        "/api/v1/diagnostics/runs/{run_id}",
        "/api/v1/prompt/verify",
        "/api/v1/prompt/models",
    ]
    for path in typed_paths:
        methods = schema["paths"][path]
        for operation in methods.values():
            if "responses" in operation and "200" in operation["responses"]:
                assert "schema" in operation["responses"]["200"]["content"]["application/json"]


@pytest.mark.asyncio
async def test_versioned_validation_errors_use_consistent_contract():
    transport = httpx.ASGITransport(app=monitor.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/prompt/verify", json={"prompt": ""})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
