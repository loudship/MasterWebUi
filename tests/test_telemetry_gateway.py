"""
tests/test_telemetry_gateway.py
================================
Regression and feature tests for the secure read-only telemetry gateway.

Coverage
--------
1.  reset_pwd.py is tombstoned (raises SystemExit, no mutation logic)
2.  reset_pwd.py contains no sqlite3.connect calls
3.  reset_pwd.py contains no UPDATE or INSERT SQL
4.  Telemetry gateway service registered in docker-compose.yml
5.  Telemetry gateway on port 127.0.0.1:19200
6.  Telemetry gateway depends on postgres and open-webui
7.  Telemetry gateway is read_only: true in compose
8.  Telemetry gateway has mem_limit: 256m
9.  Telemetry gateway has no-new-privileges:true
10. Telemetry gateway has dns: *airgap-dns binding
11. Telemetry gateway has no extra_hosts (not host-access)
12. Telemetry gateway Dockerfile exists
13. Telemetry gateway requirements.txt exists
14. Telemetry gateway app.py: token verified via hmac.compare_digest
15. Telemetry gateway app.py: no INSERT/UPDATE/DELETE SQL statements
16. Telemetry gateway app.py: DSN used verbatim, telemetry_ro role required
17. Telemetry gateway app.py: /health endpoint requires no token
18. Telemetry gateway app.py: snapshot endpoint requires X-Telemetry-Token
19. Telemetry gateway app.py: 401 on missing token
20. Telemetry gateway app.py: 401 on wrong token
21. Telemetry gateway app.py: ReadOnlyOpenWebUIClient uses only GET
22. PostgreSQL init script 002 exists
23. PostgreSQL init script 002 creates telemetry_ro role
24. PostgreSQL init script 002 grants SELECT only (no INSERT/UPDATE)
25. PostgreSQL init script 002 grants CONNECT on ops and open_webui
26. TELEMETRY_TOKEN appears in .env
27. Hardened compose policy: telemetry-gateway NOT in extra_hosts set
28. Hardened compose policy: telemetry-gateway has pull_policy: never
29. Telemetry gateway app.py CPU-only (no torch/cuda/subprocess)
30. Telemetry gateway Dockerfile is offline-buildable (no apt-get network install)
"""

from __future__ import annotations

import ast
import fastapi
import os
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DIR = ROOT / "services" / "telemetry-gateway"
INIT_SCRIPT = ROOT / "infra" / "postgres" / "init" / "002-create-telemetry-role.sh"
RESET_PWD = ROOT / "scripts" / "reset_pwd.py"
ENV_FILE = ROOT / ".env"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def _gateway_src() -> str:
    return (GATEWAY_DIR / "app.py").read_text(encoding="utf-8")


def _reset_src() -> str:
    return RESET_PWD.read_text(encoding="utf-8")


def _cpu_only(path: Path) -> None:
    forbidden = {"torch", "cuda", "subprocess"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    overlap = imports & forbidden
    assert not overlap, f"{path.name} imports forbidden: {overlap}"


# ===========================================================================
# 1-3: reset_pwd.py tombstone
# ===========================================================================


def test_reset_pwd_is_tombstoned():
    src = _reset_src()
    assert "SystemExit" in src or "raise" in src
    assert "DECOMMISSIONED" in src or "decommissioned" in src.lower() or "tombstone" in src.lower() or "TOMBSTONE" in src


def test_reset_pwd_has_no_sqlite_connect():
    src = _reset_src()
    assert "sqlite3.connect" not in src
    assert "conn.cursor()" not in src


def test_reset_pwd_has_no_mutation_sql():
    src = _reset_src().upper()
    assert "UPDATE AUTH" not in src
    assert "INSERT INTO" not in src
    assert "DELETE FROM" not in src


# ===========================================================================
# 4-11: docker-compose telemetry-gateway service
# ===========================================================================


def test_telemetry_gateway_in_compose():
    compose = _compose()
    assert "telemetry-gateway" in compose["services"]


def test_telemetry_gateway_port_19200():
    compose = _compose()
    ports = compose["services"]["telemetry-gateway"]["ports"]
    assert any("19200" in str(p) for p in ports)
    assert any("127.0.0.1" in str(p) for p in ports)


def test_telemetry_gateway_depends_on_postgres_and_open_webui():
    compose = _compose()
    deps = compose["services"]["telemetry-gateway"]["depends_on"]
    assert "postgres" in deps
    assert "open-webui" in deps


def test_telemetry_gateway_is_read_only():
    compose = _compose()
    assert compose["services"]["telemetry-gateway"].get("read_only") is True


def test_telemetry_gateway_mem_limit_256m():
    compose = _compose()
    assert compose["services"]["telemetry-gateway"].get("mem_limit") == "256m"


def test_telemetry_gateway_no_new_privileges():
    compose = _compose()
    sec = compose["services"]["telemetry-gateway"].get("security_opt", [])
    assert any("no-new-privileges" in s for s in sec)


def test_telemetry_gateway_dns_airgap():
    compose = _compose()
    dns = compose["services"]["telemetry-gateway"].get("dns", [])
    assert "127.0.0.1" in dns


def test_telemetry_gateway_no_extra_hosts():
    compose = _compose()
    assert "extra_hosts" not in compose["services"]["telemetry-gateway"]


# ===========================================================================
# 12-13: File existence
# ===========================================================================


def test_telemetry_gateway_dockerfile_exists():
    assert (GATEWAY_DIR / "Dockerfile").is_file()


def test_telemetry_gateway_requirements_exists():
    assert (GATEWAY_DIR / "requirements.txt").is_file()


# ===========================================================================
# 14-21: app.py source contracts
# ===========================================================================


def test_gateway_uses_hmac_compare_digest():
    src = _gateway_src()
    assert "hmac.compare_digest" in src


def test_gateway_has_no_write_sql():
    src = _gateway_src().upper()
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE ", "ALTER TABLE", "DROP TABLE"):
        assert forbidden not in src, f"Found write SQL: {forbidden}"


def test_gateway_requires_ro_role_and_preserves_credentials():
    import importlib.util
    import os as _os

    _os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://operator:secret@postgres:5432/ops")
    _os.environ.setdefault("TELEMETRY_TOKEN", "test-token-min-32-chars-padded-here")

    spec = importlib.util.spec_from_file_location("telemetry_gateway", GATEWAY_DIR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    # Patch asyncpg before loading to avoid real connection
    import unittest.mock as mock
    with mock.patch.dict("sys.modules", {"asyncpg": mock.MagicMock()}):
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass  # lifespan will fail without real DB — that's fine

    # The gateway must use the DSN verbatim (the old rewrite stripped the
    # password and broke authentication) and refuse non-read-only roles.
    assert mod._dsn_username("postgresql://telemetry_ro:pw@postgres:5432/ops") == "telemetry_ro"
    mod._require_ro_role("postgresql://telemetry_ro:pw@postgres:5432/ops")
    with pytest.raises(RuntimeError):
        mod._require_ro_role("postgresql://operator:password@postgres:5432/ops")
    src = _gateway_src()
    assert "_ro_dsn" not in src, "password-stripping DSN rewrite must stay deleted"


def test_gateway_health_endpoint_defined():
    src = _gateway_src()
    assert "@app.get(\"/health\")" in src


def test_gateway_snapshot_requires_token_header():
    src = _gateway_src()
    assert "X-Telemetry-Token" in src
    assert "_verify_token" in src


def test_gateway_verify_token_raises_401_on_missing():
    import importlib.util
    import os as _os
    import unittest.mock as mock

    _os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://unused@postgres/ops")
    _os.environ.setdefault("TELEMETRY_TOKEN", "test-token-min-32-chars-padded-here")

    spec = importlib.util.spec_from_file_location("telemetry_gw2", GATEWAY_DIR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"asyncpg": mock.MagicMock()}):
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass

    from fastapi import HTTPException as FHTTPException
    with pytest.raises(FHTTPException) as exc:
        mod._verify_token(None)
    assert exc.value.status_code == 401


def test_gateway_verify_token_raises_401_on_wrong_token():
    import importlib.util
    import os as _os
    import unittest.mock as mock

    _os.environ["TELEMETRY_TOKEN"] = "correct-token-that-is-32-chars-long!"

    spec = importlib.util.spec_from_file_location("telemetry_gw3", GATEWAY_DIR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"asyncpg": mock.MagicMock()}):
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass

    from fastapi import HTTPException as FHTTPException
    with pytest.raises(FHTTPException) as exc:
        mod._verify_token("wrong-token")
    assert exc.value.status_code == 401


def test_gateway_client_uses_only_get():
    src = _gateway_src()
    # The Open WebUI calls in the gateway should only use GET
    calls = re.findall(r"await client\.(get|post|put|patch|delete)\(", src)
    non_get = [c for c in calls if c != "get"]
    assert not non_get, f"Gateway makes non-GET HTTP calls: {non_get}"


# ===========================================================================
# 22-25: PostgreSQL init script
# ===========================================================================


def test_init_script_002_exists():
    assert INIT_SCRIPT.is_file()


def test_init_script_creates_telemetry_ro():
    src = INIT_SCRIPT.read_text(encoding="utf-8")
    assert "telemetry_ro" in src
    assert "CREATE ROLE" in src


def test_init_script_grants_select_only():
    src = INIT_SCRIPT.read_text(encoding="utf-8").upper()
    assert "GRANT SELECT" in src
    # Must not grant write permissions
    assert "GRANT INSERT" not in src
    assert "GRANT UPDATE" not in src
    assert "GRANT DELETE" not in src
    assert "GRANT ALL" not in src


def test_init_script_grants_connect_on_both_databases():
    src = INIT_SCRIPT.read_text(encoding="utf-8")
    assert "GRANT CONNECT ON DATABASE ops" in src
    assert "GRANT CONNECT ON DATABASE open_webui" in src


# ===========================================================================
# 26: .env has TELEMETRY_TOKEN
# ===========================================================================


def test_env_file_has_telemetry_token():
    env_text = ENV_FILE.read_text(encoding="utf-8")
    assert "TELEMETRY_TOKEN" in env_text


# ===========================================================================
# 27-28: Hardened compose policy checks
# ===========================================================================


def test_telemetry_gateway_not_in_extra_hosts_services():
    compose = _compose()
    services_with_host_access = {
        name for name, svc in compose["services"].items() if svc.get("extra_hosts")
    }
    assert "telemetry-gateway" not in services_with_host_access


def test_telemetry_gateway_pull_policy_never():
    compose = _compose()
    assert compose["services"]["telemetry-gateway"].get("pull_policy") == "never"


# ===========================================================================
# 29-30: Build hygiene
# ===========================================================================


def test_gateway_app_cpu_only():
    _cpu_only(GATEWAY_DIR / "app.py")


def test_gateway_dockerfile_offline_buildable():
    src = (GATEWAY_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "@sha256:" in src
    assert "apt-get" not in src
