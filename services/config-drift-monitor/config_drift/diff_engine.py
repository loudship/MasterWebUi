from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .security import sanitize


MISSING = object()


def pointer_get(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    current = value
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return MISSING
    return current


def normalize_url(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        parts = urlsplit(value.rstrip("/"))
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc:
        return value.rstrip("/")
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), parts.query, ""))


def content_fingerprint(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    return f"sha256:{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()}"


def values_match(expected: Any, observed: Any, mode: str) -> bool:
    if mode == "present":
        return observed is not MISSING and observed not in (None, "", [], {})
    if mode == "absent":
        return observed is MISSING or observed in (None, "", [], {})
    if observed is MISSING:
        return False
    if mode == "ordered_list":
        return list(expected or []) == list(observed or [])
    if mode == "unordered_set":
        return {json.dumps(item, sort_keys=True, default=str) for item in expected or []} == {
            json.dumps(item, sort_keys=True, default=str) for item in observed or []
        }
    if mode == "subset":
        if isinstance(expected, dict) and isinstance(observed, dict):
            return all(key in observed and values_match(child, observed[key], "subset") for key, child in expected.items())
        if isinstance(expected, list) and isinstance(observed, list):
            return all(item in observed for item in expected)
        return expected == observed
    if mode == "normalized_url":
        return normalize_url(expected) == normalize_url(observed)
    if mode == "fingerprint":
        return content_fingerprint(expected) == content_fingerprint(observed)
    return expected == observed


def build_diffs(baseline: dict[str, Any], planes: dict[str, dict[str, Any]], observed_at: float) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for rule in baseline.get("rules", []):
        for plane, source in rule["sources"].items():
            plane_state = planes.get(plane, {})
            if plane_state.get("status") != "available":
                results.append(_record(rule, plane, "unavailable", MISSING, source, observed_at, plane_state.get("error", "Plane unavailable.")))
                continue
            if "endpoint" in source:
                endpoint_data = plane_state.get("data", {}).get(source["endpoint"], MISSING)
                if endpoint_data is MISSING:
                    results.append(_record(rule, plane, "unavailable", MISSING, source, observed_at, "Required source endpoint returned no observable payload."))
                else:
                    observed = pointer_get(endpoint_data, source.get("pointer", "/"))
                    status = _comparison_status(rule, observed, "drift")
                    results.append(_record(rule, plane, status, observed, source, observed_at))
                continue
            collection = plane_state.get("data", {}).get(source.get("collection"), [])
            matched = [entity for entity in collection if _entity_selected(entity, source.get("selector"))]
            if not matched:
                results.append(_record(rule, plane, "unobservable", MISSING, source, observed_at, "No matching entity was observable."))
            for entity in matched:
                observed = pointer_get(entity, source.get("pointer", "/"))
                mismatch_status = "override" if plane in {"workspace", "user", "chat"} else "drift"
                status = "ignored" if _entity_allowlisted(rule, entity) else _comparison_status(rule, observed, mismatch_status)
                results.append(_record(rule, plane, status, observed, source, observed_at, entity_label=entity.get("entity_label", "")))
    results.extend(_dynamic_workspace_overrides(planes, observed_at))
    results.extend(_dynamic_chat_overrides(planes, observed_at))
    return sorted(results, key=lambda item: (status_rank(item["status"]), severity_rank(item["severity"]), item["logical_path"], item["entity_label"]))


def _entity_selected(entity: dict[str, Any], selector: Any) -> bool:
    if not selector or selector == "*":
        return True
    if isinstance(selector, dict):
        return all(pointer_get(entity, pointer) == expected for pointer, expected in selector.items())
    return entity.get("id") == selector


def _comparison_status(rule: dict[str, Any], observed: Any, mismatch_status: str) -> str:
    mode = rule.get("mode", "exact")
    if values_match(rule.get("expected"), observed, mode):
        return "aligned"
    if any(values_match(allowed, observed, mode) for allowed in rule.get("allowlist", [])):
        return "ignored"
    return mismatch_status


def _entity_allowlisted(rule: dict[str, Any], entity: dict[str, Any]) -> bool:
    candidates = {str(entity.get("id") or ""), str(entity.get("id_suffix") or ""), str(entity.get("entity_label") or "")}
    return any(str(allowed) in candidates for allowed in rule.get("entity_allowlist", []))


def _record(
    rule: dict[str, Any],
    plane: str,
    status: str,
    observed: Any,
    source: dict[str, Any],
    observed_at: float,
    detail: str = "",
    entity_label: str = "",
) -> dict[str, Any]:
    enforced = bool(rule.get("enforced", False))
    severity = rule.get("severity", "warning")
    recommendation = rule.get("recommendation") or (
        "Reconcile this setting through the version-controlled baseline and owning configuration plane."
        if status in {"drift", "override"}
        else ""
    )
    return {
        "rule_id": rule["id"],
        "logical_path": rule.get("logical_path", rule["id"]),
        "label": rule.get("label", rule["id"]),
        "domain": rule.get("domain", "general"),
        "parent_plane": "baseline",
        "child_plane": plane,
        "expected": sanitize(rule.get("expected")),
        "observed": None if observed is MISSING else sanitize(observed),
        "status": status,
        "severity": severity,
        "enforced": enforced,
        "entity_label": entity_label or plane.title(),
        "source_endpoint": source.get("endpoint") or source.get("collection", ""),
        "observed_at": observed_at,
        "recommendation": recommendation,
        "detail": detail,
        "provenance": rule.get("provenance", ""),
    }


def _dynamic_workspace_overrides(planes: dict[str, dict[str, Any]], observed_at: float) -> list[dict[str, Any]]:
    admin = planes.get("admin", {}).get("data", {}).get("models_config", {})
    defaults = admin.get("DEFAULT_MODEL_PARAMS") or {}
    models = planes.get("workspace", {}).get("data", {}).get("models", [])
    return _dynamic_param_overrides(defaults, models, "admin", "workspace", observed_at)


def _dynamic_chat_overrides(planes: dict[str, dict[str, Any]], observed_at: float) -> list[dict[str, Any]]:
    admin_defaults = planes.get("admin", {}).get("data", {}).get("models_config", {}).get("DEFAULT_MODEL_PARAMS") or {}
    model_map = {model.get("id"): {**admin_defaults, **(model.get("params") or {})} for model in planes.get("workspace", {}).get("data", {}).get("models", [])}
    results: list[dict[str, Any]] = []
    for chat in planes.get("chat", {}).get("data", {}).get("chats", []):
        models = chat.get("models") or []
        parent = model_map.get(models[0], admin_defaults) if models else admin_defaults
        results.extend(_dynamic_param_overrides(parent, [chat], "workspace", "chat", observed_at))
    return results


def _dynamic_param_overrides(parent: dict[str, Any], entities: list[dict[str, Any]], parent_plane: str, child_plane: str, observed_at: float) -> list[dict[str, Any]]:
    results = []
    for entity in entities:
        for key, observed in (entity.get("params") or {}).items():
            expected = parent.get(key, MISSING)
            if expected is MISSING or expected == observed:
                continue
            results.append(
                {
                    "rule_id": f"dynamic.{child_plane}.params.{key}",
                    "logical_path": f"models.params.{key}",
                    "label": f"{child_plane.title()} parameter override: {key}",
                    "domain": "models",
                    "parent_plane": parent_plane,
                    "child_plane": child_plane,
                    "expected": sanitize(expected),
                    "observed": sanitize(observed),
                    "status": "override",
                    "severity": "warning",
                    "enforced": False,
                    "entity_label": entity.get("entity_label", child_plane.title()),
                    "source_endpoint": "params",
                    "observed_at": observed_at,
                    "recommendation": "Confirm this lower-plane override is intentional or remove it from the owning UI plane.",
                    "detail": "Automatically detected lower-plane override.",
                    "provenance": "runtime hierarchy",
                }
            )
    return results


def status_rank(status: str) -> int:
    return {"unavailable": 0, "drift": 1, "override": 2, "unobservable": 3, "aligned": 4, "ignored": 5}.get(status, 9)


def severity_rank(severity: str) -> int:
    return {"critical": 0, "warning": 1, "info": 2}.get(severity, 9)
