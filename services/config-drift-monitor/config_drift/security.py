from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE = re.compile(
    r"(authorization|api[-_]?key|token|secret|password|passwd|cookie|credential|requirepass|email)",
    re.IGNORECASE,
)
CONTENT_LIKE = re.compile(r"(prompt|template|system|content|message|history|files?|title)", re.IGNORECASE)


def fingerprint(value: str) -> dict[str, Any]:
    return {
        "fingerprint": f"sha256:{hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()}",
        "length": len(value),
    }


def sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc:
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    query = urlencode(
        [
            (key, "[REDACTED]" if SENSITIVE.search(key) else child)
            for key, child in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def sanitize(value: Any, key_hint: str = "") -> Any:
    if SENSITIVE.search(key_hint):
        if isinstance(value, bool):
            return value
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(key): sanitize(child, str(key)) for key, child in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(child, key_hint) for child in value]
    if isinstance(value, str):
        if CONTENT_LIKE.search(key_hint) or len(value) > 512:
            return fingerprint(value)
        return sanitize_url(value)
    return value


def user_label(user: dict[str, Any]) -> str:
    name = str(user.get("name") or "User")
    suffix = str(user.get("id") or "")[-8:]
    return f"{name} · {suffix}" if suffix else name


def chat_label(chat_id: str, updated_at: int | float | None) -> str:
    return f"Chat · {str(chat_id)[-8:]} · {int(updated_at or 0)}"
