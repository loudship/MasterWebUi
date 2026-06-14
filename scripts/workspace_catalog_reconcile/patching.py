"""Idempotent source transforms for legacy live-only Workspace tools."""

from __future__ import annotations

import re


def patch_sandbox(content: str) -> str:
    for field in ("NETWORKING_ALLOWED", "AUTO_INSTALL", "CHECK_FOR_UPDATES"):
        false_pattern = rf"{field}: bool = pydantic\.Field\(\s*default=False"
        if re.search(false_pattern, content):
            continue
        true_pattern = rf"({field}: bool = pydantic\.Field\(\s*default=)True"
        content, count = re.subn(true_pattern, rf"\1False", content, count=1)
        if count != 1:
            raise RuntimeError(f"Could not set safe default for sandbox field {field}")
    return content


def patch_inline_visualizer_local_only(content: str) -> str:
    pattern = re.compile(
        r'_KNOWN_CDNS = \(\s*"https://cdnjs\.cloudflare\.com" '
        r'" https://cdn\.jsdelivr\.net" " https://unpkg\.com"\s*\)'
    )
    content, count = pattern.subn('_KNOWN_CDNS = ""', content, count=1)
    if count == 0 and '_KNOWN_CDNS = ""' not in content:
        raise RuntimeError("Could not remove the Inline Visualizer CDN allowlist")
    return content


def patch_mcp_url_guard(content: str) -> str:
    guard = (
        '        if not self.valves.mcp_server_url.strip().startswith(("http://", "https://")):\n'
        '            return "Configuration error: set mcp_server_url to an http:// or https:// endpoint."\n'
    )
    for method_name in ("list_mcp_tools", "call_mcp_tool"):
        pattern = re.compile(
            rf"(?m)^(    async def {method_name}\([^\n]*\) -> [^:\n]+:\n"
            rf"|    async def {method_name}\((?:[^\n]*\n)*?    \) -> [^:\n]+:\n)"
        )
        match = pattern.search(content)
        if not match:
            raise RuntimeError(f"Could not find MCP bridge method {method_name}")
        following = content[match.end() : match.end() + len(guard) + 40]
        if "Configuration error: set mcp_server_url" not in following:
            content = pattern.sub(rf"\1{guard}", content, count=1)
    return content


def remove_python_method(content: str, method_name: str) -> str:
    if not re.search(rf"(?m)^    (?:async )?def {re.escape(method_name)}\(", content):
        return content
    pattern = re.compile(
        rf"(?ms)^    (?:async )?def {re.escape(method_name)}\(.*?(?=^    (?:async )?def |\Z)"
    )
    content, count = pattern.subn("", content, count=1)
    if count != 1:
        raise RuntimeError(f"Could not remove method {method_name}")
    return content.rstrip() + "\n"
