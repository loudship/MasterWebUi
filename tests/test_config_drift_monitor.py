import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "config-drift-monitor"
sys.path.insert(0, str(SERVICE))

import app as drift_app
from config_drift.baseline import BaselineError, BaselineLoader, validate_baseline
from config_drift.diff_engine import MISSING, build_diffs, pointer_get, values_match
from config_drift.security import sanitize
from config_drift.telemetry import DriftEngine, ReadOnlyOpenWebUIClient


def minimal_baseline():
    return {
        "schema_version": 1,
        "metadata": {"name": "test"},
        "rules": [
            {
                "id": "admin.direct_connections",
                "logical_path": "admin.connections.direct",
                "label": "Direct connections",
                "domain": "connections",
                "severity": "critical",
                "enforced": True,
                "mode": "exact",
                "expected": False,
                "sources": {"admin": {"endpoint": "connections", "pointer": "/ENABLE_DIRECT_CONNECTIONS"}},
            }
        ],
    }


def write_baseline(tmp_path, data=None):
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump(data or minimal_baseline()), encoding="utf-8")
    return BaselineLoader(str(path))


def test_baseline_contract_validates_modes_and_rejects_literal_secrets(tmp_path):
    baseline = BaselineLoader(str(ROOT / "config" / "config-drift-baseline.yaml")).load()
    assert baseline["metadata"]["precedence"] == "hardened-compose-over-legacy-master-config"
    assert len(baseline["rules"]) >= 10

    invalid = minimal_baseline()
    invalid["rules"][0] = {
        "id": "admin.secret",
        "logical_path": "admin.secret",
        "mode": "exact",
        "expected": "sk-literal-secret",
        "sources": {"admin": {"endpoint": "auth", "pointer": "/secret"}},
    }
    with pytest.raises(BaselineError):
        validate_baseline(invalid)

    invalid_allowlist = minimal_baseline()
    invalid_allowlist["rules"][0]["allowlist"] = "not-a-list"
    with pytest.raises(BaselineError):
        validate_baseline(invalid_allowlist)


def test_pointer_and_comparison_modes():
    assert pointer_get({"a": {"b": 2}}, "/a/b") == 2
    assert pointer_get({}, "/missing") is MISSING
    assert values_match([1, 2], [2, 1], "unordered_set")
    assert not values_match([1, 2], [2, 1], "ordered_list")
    assert values_match({"a": 1}, {"a": 1, "b": 2}, "subset")
    assert values_match("HTTP://EXAMPLE.COM/path/", "http://example.com/path", "normalized_url")
    assert values_match("same content", "same content", "fingerprint")


def test_diff_classification_and_unavailable_plane():
    baseline = minimal_baseline()
    planes = {
        "admin": {"status": "available", "data": {"connections": {"ENABLE_DIRECT_CONNECTIONS": True}}},
        "workspace": {"status": "available", "data": {"models": []}},
        "user": {"status": "available", "data": {"users": []}},
        "chat": {"status": "available", "data": {"chats": []}},
    }
    diffs = build_diffs(baseline, planes, time.time())
    assert diffs[0]["status"] == "drift"
    assert diffs[0]["severity"] == "critical"

    planes["admin"] = {"status": "unavailable", "error": "HTTP 401", "data": {}}
    assert build_diffs(baseline, planes, time.time())[0]["status"] == "unavailable"

    planes["admin"] = {"status": "available", "data": {}}
    assert build_diffs(baseline, planes, time.time())[0]["status"] == "unavailable"

    allowlisted = minimal_baseline()
    allowlisted["rules"][0]["allowlist"] = [True]
    planes["admin"] = {"status": "available", "data": {"connections": {"ENABLE_DIRECT_CONNECTIONS": True}}}
    assert build_diffs(allowlisted, planes, time.time())[0]["status"] == "ignored"


def test_sanitizer_redacts_credentials_fingerprints_content_and_removes_url_auth():
    result = sanitize(
        {
            "api_key": "secret",
            "authorization": "Bearer secret",
            "admin_email": "private@example.com",
            "prompt": "private prompt",
            "url": "http://user:pass@example.com/path?api_key=query-secret&safe=value",
            "authentication_configured": True,
        }
    )
    encoded = json.dumps(result)
    assert "secret" not in encoded
    assert "private@example.com" not in encoded
    assert "user:pass" not in encoded
    assert "query-secret" not in encoded
    assert result["prompt"]["fingerprint"].startswith("sha256:")
    assert result["authentication_configured"] is True


class FakeClient:
    def __init__(self):
        self.methods = []
        self.active_details = 0
        self.max_active_details = 0

    async def get(self, path):
        self.methods.append(("GET", path))
        if path == "/api/v1/users/":
            return {"users": [{"id": "user-12345678", "name": "Operator", "email": "private@example.com", "role": "admin", "settings": {"ui": {"theme": "dark"}}}], "total": 1}
        if path == "/api/v1/users/default/permissions":
            return {"chat": {"controls": True}}
        if path.startswith("/api/v1/chats/list/user/"):
            now = int(time.time())
            return [{"id": f"chat-{index:08d}", "title": f"private title {index}", "updated_at": now, "created_at": now} for index in range(5)]
        if path.startswith("/api/v1/chats/chat-"):
            self.active_details += 1
            self.max_active_details = max(self.max_active_details, self.active_details)
            await asyncio.sleep(0.01)
            self.active_details -= 1
            return {"chat": {"models": ["model"], "params": {"temperature": 0.4}, "system": "private system", "messages": ["private message"], "history": {"private": True}, "files": ["secret"]}}
        if path == "/api/v1/models/export":
            return [{"id": "model", "params": {"temperature": 0.2}, "meta": {"description": "private model description"}}]
        if path == "/api/v1/models/list":
            return {"items": [], "total": 1}
        if path == "/api/v1/models/base":
            return [{"id": "base"}]
        return {"ENABLE_DIRECT_CONNECTIONS": False, "DEFAULT_MODEL_PARAMS": {"temperature": 0.1}}

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_chat_polling_is_bounded_and_immediately_discards_content(tmp_path):
    fake = FakeClient()
    engine = DriftEngine(fake, write_baseline(tmp_path), chats_per_user=5, max_chats=5)
    await engine.poll_users()
    await engine.poll_chats()
    encoded = json.dumps(engine.planes["chat"])
    assert fake.max_active_details == 2
    assert "private title" not in encoded
    assert "private message" not in encoded
    assert "history" not in encoded
    assert "files" not in encoded
    assert "private system" not in encoded
    assert len(engine.planes["chat"]["data"]["chats"]) == 5


@pytest.mark.asyncio
async def test_engine_builds_hierarchy_overrides_and_client_contract_is_get_only(tmp_path):
    fake = FakeClient()
    engine = DriftEngine(fake, write_baseline(tmp_path))
    await engine.refresh_all()
    assert all(method == "GET" for method, _ in fake.methods)
    assert any(item["status"] == "override" and item["child_plane"] == "workspace" for item in engine.diffs)
    assert any(item["status"] == "override" and item["child_plane"] == "chat" for item in engine.diffs)
    assert not hasattr(ReadOnlyOpenWebUIClient, "post")
    telemetry = (SERVICE / "config_drift" / "telemetry.py").read_text(encoding="utf-8").lower()
    for forbidden in (".post(", ".put(", ".patch(", ".delete("):
        assert forbidden not in telemetry

    engine.planes["user"]["observed_at"] = time.time() - 1000
    assert next(item for item in engine.overview()["planes"] if item["name"] == "user")["status"] == "stale"
    assert engine.overview()["status"] == "warning"
    stale_snapshot = engine.snapshot()
    assert stale_snapshot["planes"]["user"]["status"] == "stale"
    assert stale_snapshot["planes"]["user"]["data"] == {}


@pytest.mark.asyncio
async def test_user_polling_fetches_all_pages(tmp_path):
    class PagedFakeClient(FakeClient):
        async def get(self, path):
            self.methods.append(("GET", path))
            if path == "/api/v1/users/":
                return {"users": [{"id": "user-00000001", "name": "One"}], "total": 2}
            if path == "/api/v1/users/?page=2":
                return {"users": [{"id": "user-00000002", "name": "Two"}], "total": 2}
            if path == "/api/v1/users/default/permissions":
                return {"chat": {"controls": True}}
            return await super().get(path)

    engine = DriftEngine(PagedFakeClient(), write_baseline(tmp_path))
    await engine.poll_users()
    assert engine.planes["user"]["item_count"] == 2
    assert len(engine._raw_user_refs) == 2


class ApiFakeEngine:
    generated_at = 1.0
    event_version = 3
    diffs = []
    baseline_error = ""

    def overview(self):
        return {"status": "aligned", "counts": {"aligned": 0, "override": 0, "drift": 0, "unavailable": 0, "unobservable": 0, "ignored": 0}, "planes": [], "baseline": {"valid": True, "rule_count": 1, "schema_version": 1, "metadata": {}, "error": ""}, "generated_at": 1.0, "event_version": 3}

    def snapshot(self):
        return {"overview": self.overview(), "planes": {}, "diffs": []}

    def baseline_public(self):
        return minimal_baseline()

    async def refresh_all(self, manual=False):
        return self.snapshot()


@pytest.mark.asyncio
async def test_typed_api_sse_exports_and_consistent_errors(monkeypatch):
    monkeypatch.setattr(drift_app, "engine", ApiFakeEngine())
    transport = httpx.ASGITransport(app=drift_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/overview")).status_code == 200
        assert (await client.get("/api/v1/snapshot")).status_code == 200
        assert "# Configuration Drift Monitor Snapshot" in (await client.get("/api/v1/export?format=markdown")).text
        invalid = await client.get("/api/v1/export?format=bad")
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "validation_error"

    class FakeRequest:
        async def is_disconnected(self):
            return False

    response = await drift_app.events(FakeRequest())
    first = await response.body_iterator.__anext__()
    await response.body_iterator.aclose()
    assert "event: update" in first


def test_ui_and_container_hardening_contracts():
    html = (SERVICE / "ui" / "index.html").read_text(encoding="utf-8")
    js = (SERVICE / "ui" / "app.js").read_text(encoding="utf-8")
    dockerfile = (SERVICE / "Dockerfile").read_text(encoding="utf-8").lower()
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for label in ("Alignment Overview", "Differential Matrix", "Plane Inspector", "Baseline Contract", "Export & Diagnostics"):
        assert label in html
    assert 'role="tooltip"' in html
    assert 'rel="icon"' in html
    assert "pointer-events:none" in (SERVICE / "ui" / "styles.css").read_text(encoding="utf-8")
    assert "EventSource" in js and "60000" in js
    for forbidden in ("torch", "sentence-transformers", "qdrant", "redis", "sqlite"):
        assert forbidden not in dockerfile
    for expected in ("127.0.0.1:19100:9100", "mem_limit: 512m", 'cpus: "1.0"', "pids_limit: 64", "read_only: true", "no-new-privileges:true"):
        assert expected in compose
    assert "Configuration Drift" in (ROOT / "monitor_ui" / "index.html").read_text(encoding="utf-8")
