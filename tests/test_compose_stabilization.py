"""
tests/test_compose_stabilization.py
===================================
Pins for the compose/frontend stabilization decisions (audit P3-1, P3-3):

1.  WebSocket transport enabled — long-polling degraded every socket.io tool
    status event under load; all binds are loopback-only so it adds no exposure.
2.  Brutalist artifact CSS no longer uses a universal descendant selector.
3.  desktop_eye routes through the inference gateway, not LM Studio directly.
4.  The open-webui sandbox privileges (SYS_ADMIN + cgroup rw) stay EXACTLY as
    the gVisor code-execution blueprint requires — documented as deliberate,
    not drift (audit P3-4 resolution).
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_websocket_support_enabled():
    env = _compose()["services"]["open-webui"]["environment"]
    assert env["ENABLE_WEBSOCKET_SUPPORT"] == "true"


def test_brutalist_css_has_no_universal_descendant_selector():
    override = ROOT / "workspace" / "open-webui-overrides"
    assert not (override / "patch_frontend.mjs").exists(), "patch_frontend.mjs should be deleted."
    assert not (override / "src").exists(), "Frontend overrides src folder should be deleted"


def test_desktop_eye_routes_through_gateway():
    src = (ROOT / "ops" / "desktop_eye.py").read_text(encoding="utf-8")
    assert "127.0.0.1:4322" in src, "vision calls must pass the gateway allowlist + GPU lock"
    assert 'VISION_ENDPOINT", "http://localhost:4321' not in src
    assert "503" in src, "gateway-busy responses must skip the cycle gracefully"


def test_open_webui_sandbox_privileges_unchanged():
    """gVisor code-execution sandbox requires these (blueprint §5.1) — they are
    deliberate, pinned here so removal is a conscious decision."""
    service = _compose()["services"]["open-webui"]
    assert service["cap_add"] == ["SYS_ADMIN"]
    assert "seccomp=unconfined" in service["security_opt"]
    assert "no-new-privileges:true" in service["security_opt"]
