import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_one_locked_network_and_no_wan_routes():
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    assert list(compose["networks"]) == ["llm-net"]
    assert (
        compose["networks"]["llm-net"]["driver_opts"][
            "com.docker.network.bridge.enable_ip_masquerade"
        ]
        == "false"
    )
    for forbidden in ("tor-gateway", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "network_mode", "internal: false"):
        assert forbidden not in compose_text
    assert all(service.get("dns") == ["127.0.0.1"] for service in compose["services"].values())


def test_only_gateway_has_host_access():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services_with_host_access = {
        name for name, service in compose["services"].items() if service.get("extra_hosts")
    }
    assert services_with_host_access == {"inference-gateway"}


def test_non_build_images_are_digest_pinned_or_reuse_a_local_build():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    local_build_images = {
        service["image"] for service in compose["services"].values() if service.get("build")
    }
    for service in compose["services"].values():
        image = service.get("image", "")
        assert "@sha256:" in image or image in local_build_images
        assert service.get("pull_policy") == "never"


def test_core_services_wait_for_postgres_and_qdrant_health():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for service_name in ("inference-gateway", "langgraph-orchestrator", "pipelines", "open-webui"):
        depends_on = compose["services"][service_name]["depends_on"]
        assert depends_on["postgres"]["condition"] == "service_healthy"
        assert depends_on["qdrant"]["condition"] == "service_healthy"


def test_orchestrator_uses_alias_only_and_three_loop_contract():
    source = (ROOT / "backend" / "langgraph_orchestrator.py").read_text(encoding="utf-8")
    assert "TOTAL_ALLOWED_LOOPS = 3" in source
    assert "MemorySaver" not in source
    assert "AsyncPostgresSaver" in source
    assert 'collection_name="' not in source
    assert source.count("collection_name=QDRANT_NARRATIVE_ALIAS") == 2
    assert "fail_safe_termination" in source


def test_hitl_redis_client_only_queues_and_closes():
    tree = ast.parse((ROOT / "backend" / "hitl_broker.py").read_text(encoding="utf-8"))
    calls = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if isinstance(target, ast.Attribute) and target.attr == "_client":
            calls.add(node.func.attr)
    assert calls <= {"blpop", "lpush", "aclose"}


def test_only_orchestrator_receives_redis_url():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    recipients = {
        name
        for name, service in compose["services"].items()
        if "REDIS_URL" in service.get("environment", {})
    }
    assert recipients == {"langgraph-orchestrator"}


def test_pipelines_mounts_only_the_hardened_router():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    mounts = compose["services"]["pipelines"]["volumes"]
    assert mounts == ["./pipelines/langgraph_router.py:/app/pipelines/langgraph_router.py:ro"]


def test_runtime_model_access_is_gateway_only():
    for relative in (
        "backend/langgraph_orchestrator.py",
        "pipelines/langgraph_router.py",
        "pipelines/qdrant_segregated_memory.py",
        "services/langgraph-orchestrator/Dockerfile",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "LM_STUDIO" not in source
        assert "host.docker.internal" not in source


def test_runtime_has_no_tor_or_proxy_route_switches():
    for relative in (
        "docker-compose.yml",
        "deep-web-mcp/server.py",
        "deep-web-mcp/extractor.py",
        "monitor_daemon.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8").lower()
        for forbidden in ("tor-gateway", "use_tor", "privoxy", "proxy_config"):
            assert forbidden not in source


def test_custom_runtime_images_build_without_package_network_access():
    for relative in (
        "services/inference-gateway/Dockerfile",
        "services/langgraph-orchestrator/Dockerfile",
        "deep-web-mcp/Dockerfile",
        "Dockerfile.monitor",
        "migration/Dockerfile",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "@sha256:" in source
        assert "apt-get" not in source
        assert "playwright install" not in source
        if "pip install" in source:
            assert "--no-index" in source


def test_monitor_requires_alias_and_never_creates_collection():
    source = (ROOT / "monitor_daemon.py").read_text(encoding="utf-8")
    assert 'f"{QDRANT_URL}/aliases"' in source
    assert "_require_collection_alias" in source
    assert "_ensure_collection" not in source
    assert "create_body" not in source


def test_legacy_memory_filter_has_no_export_or_collection_recreation_path():
    source = (ROOT / "pipelines" / "qdrant_segregated_memory.py").read_text(encoding="utf-8")
    assert "LANGFUSE" not in source
    assert "/api/public/ingestion" not in source
    assert "session.delete(" not in source
    assert "Runtime collection reset is forbidden" in source
