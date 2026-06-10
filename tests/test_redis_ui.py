from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_redis_ui_is_offline_buildable_and_local_management_only():
    dockerfile = (ROOT / "services" / "redis-ui" / "Dockerfile").read_text(encoding="utf-8")
    source = (ROOT / "services" / "redis-ui" / "redis_ui.py").read_text(encoding="utf-8")
    assert "@sha256:" in dockerfile
    assert "--no-index" in dockerfile
    assert "apt-get" not in dockerfile
    assert "flushall" not in source.lower()
    assert "flushdb" not in source.lower()
    assert "confirmation != key" in source
    assert "MAX_VALUE_BYTES" in source


def test_redis_ui_has_operational_and_visual_debugging_views():
    html = (ROOT / "services" / "redis-ui" / "redis_ui.html").read_text(encoding="utf-8")
    for expected in (
        "Overview",
        "Key browser",
        "Debug",
        "Operations activity",
        "Database key distribution",
        "Top commands",
        "Connected clients",
        "Slow log",
        "Safety and configuration",
    ):
        assert expected in html
