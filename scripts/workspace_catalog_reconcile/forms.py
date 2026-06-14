"""Pure catalog form derivation and comparison helpers."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

BUILTIN_TOOL_CATEGORIES = (
    "time",
    "memory",
    "chats",
    "notes",
    "knowledge",
    "channels",
    "web_search",
    "image_generation",
    "code_interpreter",
    "tasks",
    "automations",
    "calendar",
)


def model_form(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": model["id"],
        "base_model_id": model.get("base_model_id"),
        "name": model["name"],
        "meta": model.get("meta") or {},
        "params": model.get("params") or {},
        "access_grants": model.get("access_grants") or [],
        "is_active": model.get("is_active", True),
    }


def model_seed(models: dict[str, dict[str, Any]], item_id: str, desired: dict[str, Any]) -> dict[str, Any]:
    current = models.get(item_id)
    if current:
        return current
    clone_from = desired.get("clone_from")
    if not clone_from:
        raise RuntimeError(f"Required model is missing: {item_id}")
    source = models.get(clone_from)
    if not source:
        raise RuntimeError(f"Model {item_id} requires missing clone source: {clone_from}")
    return {**copy.deepcopy(source), "id": item_id, "name": desired["name"]}


def desired_model(current: dict[str, Any], desired: dict[str, Any], knowledge: dict[str, Any]) -> dict[str, Any]:
    result = model_form(copy.deepcopy(current))
    result["name"] = desired["name"]
    meta = result["meta"]
    if desired.get("description"):
        meta["description"] = desired["description"]
    else:
        meta.pop("description", None)
    meta["toolIds"] = desired.get("tool_ids", [])
    meta["skillIds"] = desired.get("skill_ids", [])
    meta["filterIds"] = desired.get("filter_ids", [])
    meta["defaultFilterIds"] = desired.get("default_filter_ids", [])
    meta["defaultFeatureIds"] = desired.get("default_features", [])
    meta["tags"] = [{"name": tag} for tag in desired.get("tags", [])]
    if desired.get("catalog"):
        meta["catalog"] = copy.deepcopy(desired["catalog"])
    capabilities = {key: False for key in (meta.get("capabilities") or {})}
    for key in desired.get("capabilities", []):
        capabilities[key] = True
    meta["capabilities"] = capabilities
    allowed_builtin_tools = set(desired.get("builtin_tools", []))
    if capabilities.get("builtin_tools"):
        meta["builtinTools"] = {
            category: False
            for category in BUILTIN_TOOL_CATEGORIES
            if category not in allowed_builtin_tools
        }
    else:
        meta.pop("builtinTools", None)
    # First-class persona/roleplay support: declarative knowledge + system_suppressed + recommended_voice + auto call.
    # Removes need for scattered remove_params + knowledge_* special cases for moyclark/Galadriel.
    kcfg = desired.get("knowledge") or {}
    if kcfg.get("ids"):
        meta["knowledge"] = [
            {
                "id": item_id,
                "name": knowledge["name"],
                "type": "collection",
                **({"context": kcfg.get("context")} if kcfg.get("context") else {}),
            }
            for item_id in kcfg["ids"]
        ]
    else:
        meta.pop("knowledge", None)

    if desired.get("type"):
        meta["type"] = desired["type"]
    if desired.get("recommended_voice"):
        meta.setdefault("tts", {})["voice"] = desired["recommended_voice"]
    if desired.get("auto_enable_call_mode"):
        meta["call"] = {**(meta.get("call") or {}), "auto": True}

    result["params"].update(desired.get("params", {}))
    if desired.get("system_suppressed"):
        result["params"].pop("system", None)
    for key in desired.get("remove_params", []):
        result["params"].pop(key, None)
    return result


def tool_form(
    tool: dict[str, Any],
    name: str | None = None,
    content: str | None = None,
    catalog_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = tool.get("meta") or {}
    manifest = copy.deepcopy(meta.get("manifest") or {})
    if catalog_meta:
        manifest["catalog"] = catalog_meta
    risk = (catalog_meta or {}).get("risk", "read-only")
    existing_tags = [tag for tag in (meta.get("tags") or []) if not str(tag).startswith("risk:")]
    return {
        "id": tool["id"],
        "name": name or tool["name"],
        "content": content or tool["content"],
        "meta": {
            "description": meta.get("description") or "",
            "manifest": manifest,
            "tags": existing_tags + [f"risk:{risk}"],
        },
        "access_grants": tool.get("access_grants") or [],
    }


def function_form(root: Path, item_id: str, desired: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": desired["name"],
        "content": (root / desired["source"]).read_text(encoding="utf-8"),
        "meta": {
            "description": desired["name"],
            "catalog": {
                "risk": desired.get("risk", "read-only"),
                "dependency_health": "healthy",
                "validation_status": "passed",
                "details": "CPU-only native Filter with bounded local processing.",
            },
        },
    }
