from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_open_webui_grants_only_required_namespace_privilege():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["open-webui"]
    assert service["cap_add"] == ["SYS_ADMIN"]
    assert "seccomp=unconfined" in service["security_opt"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert "/sys/fs/cgroup:/sys/fs/cgroup:rw" in service["volumes"]
    assert service.get("privileged") is not True


def test_live_calendar_overlay_uses_one_required_password_variable():
    compose = yaml.safe_load((ROOT / "docker-compose.calendar.yml").read_text(encoding="utf-8"))
    database = compose["services"]["calendar-db"]["environment"]
    mcp = compose["services"]["calendar-mcp"]["environment"]
    assert database["POSTGRES_PASSWORD"].startswith("${CALENDAR_DB_PASSWORD:")
    assert "${CALENDAR_DB_PASSWORD:" in mcp["DB_POSTGRES_URL"]
    assert "calendar-db:5432/calendar_db" in mcp["DB_POSTGRES_URL"]
    assert compose["networks"]["live-llm-net"]["external"] is True


def test_google_calendar_credentials_overlay_is_external_and_read_only():
    compose = yaml.safe_load((ROOT / "docker-compose.calendar.google.yml").read_text(encoding="utf-8"))
    service = compose["services"]["calendar-mcp"]
    assert service["environment"]["GOOGLE_APPLICATION_CREDENTIALS"] == "/run/secrets/google-calendar-adc.json"
    mount = service["volumes"][0]
    assert mount.startswith("${GOOGLE_APPLICATION_CREDENTIALS_HOST_PATH:")
    assert mount.endswith(":/run/secrets/google-calendar-adc.json:ro")
