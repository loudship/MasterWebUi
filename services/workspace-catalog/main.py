"""
Standalone Workspace Catalog Status Service.

Derives catalog risk, dependency-health, and validation metadata for all
workspace items (models, tools, functions, skills, prompts, knowledge) by
reading from Open WebUI's public REST API. Runs as a separate container —
no Open WebUI source patching required.

Environment variables
---------------------
OWUI_BASE_URL Internal URL of the Open WebUI service (default: http://open-webui:8080).
OWUI_ADMIN_TOKEN Admin API token issued by Open WebUI (required).
PORT Listen port (default: 9080).
"""
from __future__ import annotations

import os
import re
import logging
from collections import Counter
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

OWUI_BASE_URL: str = os.getenv("OWUI_BASE_URL", "http://open-webui:8080")
OWUI_ADMIN_TOKEN: str = os.getenv("OWUI_ADMIN_TOKEN", "")

app = FastAPI(
    title="Workspace Catalog",
    description="Catalog risk and dependency status for workspace items.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Risk classification and helpers
# ---------------------------------------------------------------------------
CatalogKind = Literal["model", "knowledge", "prompt", "skill", "tool", "function"]
RiskLevel = Literal["read-only", "state-changing", "external-network", "operator-only"]
HealthLevel = Literal["healthy", "warning", "unknown"]
ValidationLevel = Literal["passed", "warning", "failed", "not-validated"]

_OPERATOR_IDS = {"run_code_py", "swarm_controls", "deep_web_advanced_tools", "mcp_app_bridge"}
_EXTERNAL_IDS = {"youtube_transcript_provider"}
_STATEFUL_WORDS = re.compile(
    r"\b(create|delete|update|write|call_service|start|evict|invoke|run_)\b", re.I
)


def _risk(item_id: str, kind: CatalogKind, specs: list[dict] | None, catalog: dict, description: str = "") -> RiskLevel:
    configured = catalog.get("risk")
    if configured in {"read-only", "state-changing", "external-network", "operator-only"}:
        return configured
    if item_id in _OPERATOR_IDS:
        return "operator-only"
    if item_id in _EXTERNAL_IDS:
        return "external-network"
    if kind != "tool" and kind != "function":
        return "read-only"
    if specs:
        names = " ".join(str(spec.get("name", "")) for spec in specs)
        if _STATEFUL_WORDS.search(names):
            return "state-changing"
    if _STATEFUL_WORDS.search(description or ""):
        return "state-changing"
    return "read-only"


def _category(name: str, kind: CatalogKind) -> str:
    parts = [part.strip() for part in (name or "").split(" - ") if part.strip()]
    if len(parts) > 1 and parts[0].lower() in {"preset", "tool", "skill", "prompt", "knowledge"}:
        return parts[1]
    return kind.replace("-", " ").title()


def _validation(catalog: dict) -> tuple[ValidationLevel, int | None]:
    status = str(catalog.get("validation_status", "not-validated")).replace("_", "-")
    if status not in {"passed", "warning", "failed", "not-validated"}:
        status = "not-validated"
    last_validated_at = catalog.get("last_validated_at")
    return status, last_validated_at if isinstance(last_validated_at, int) else None


def _version(meta: Any, catalog: dict) -> str | None:
    if not isinstance(meta, dict):
        return None
    manifest = meta.get("manifest", {}) if isinstance(meta, dict) else {}
    value = catalog.get("version") or (manifest.get("version") if isinstance(manifest, dict) else None)
    return str(value) if value else None


def _catalog_meta(meta: Any) -> dict[str, Any]:
    if isinstance(meta, dict):
        manifest = meta.get("manifest")
        if isinstance(manifest, dict) and isinstance(manifest.get("catalog"), dict):
            return manifest["catalog"]
        catalog = meta.get("catalog")
        return catalog if isinstance(catalog, dict) else {}
    return {}


class CatalogItem(BaseModel):
    id:                str
    name:              str
    kind:              CatalogKind
    category:          str
    risk:              RiskLevel
    dependency_health: HealthLevel       = "unknown"
    attachment_count:  int               = 0
    version:           str | None        = None
    validation_status: ValidationLevel   = "not-validated"
    last_validated_at: int | None        = None
    details:           str               = ""


# ---------------------------------------------------------------------------
# OWUI API helpers
# ---------------------------------------------------------------------------
def _owui_headers() -> dict[str, str]:
    if not OWUI_ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="OWUI_ADMIN_TOKEN not configured.")
    return {"Authorization": f"Bearer {OWUI_ADMIN_TOKEN}"}


async def _get_owui(client: httpx.AsyncClient, path: str) -> list[dict]:
    try:
        response = await client.get(f"{OWUI_BASE_URL}{path}", headers=_owui_headers(), timeout=10.0)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else data.get("items", data.get("data", []))
    except Exception as exc:
        logger.warning("[OWUI] GET %s failed: %s", path, exc)
        return []


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------
@app.get("/status", response_model=list[CatalogItem])
async def catalog_status():
    """Return catalog status for all workspace items."""
    async with httpx.AsyncClient(trust_env=False) as client:
        tools     = await _get_owui(client, "/api/v1/tools/")
        functions = await _get_owui(client, "/api/v1/functions/")
        models    = await _get_owui(client, "/api/v1/models/list")
        skills    = await _get_owui(client, "/api/v1/skills/")
        prompts   = await _get_owui(client, "/api/v1/prompts/")
        knowledge = await _get_owui(client, "/api/v1/knowledge/")

    attachment_counts: Counter[str] = Counter()
    for model in models:
        meta = model.get("meta") or {}
        for key in ("toolIds", "filterIds", "skillIds"):
            for item_id in meta.get(key, []) or []:
                attachment_counts[str(item_id)] += 1
        for item in meta.get("knowledge", []) or []:
            if isinstance(item, dict) and item.get("id"):
                attachment_counts[str(item["id"])] += 1

    existing_tools = {t.get("id") for t in tools if t.get("id")}
    existing_skills = {s.get("id") for s in skills if s.get("id")}
    existing_knowledge = {k.get("id") for k in knowledge if k.get("id")}
    
    items: list[CatalogItem] = []

    for model in models:
        meta = model.get("meta") or {}
        catalog = _catalog_meta(meta)
        missing = [
            item_id
            for item_id in (meta.get("toolIds", []) or [])
            if item_id not in existing_tools
        ]
        missing += [
            item_id
            for item_id in (meta.get("skillIds", []) or [])
            if item_id not in existing_skills
        ]
        missing += [
            item.get("id")
            for item in (meta.get("knowledge", []) or [])
            if isinstance(item, dict) and item.get("id") not in existing_knowledge
        ]
        validation_status, last_validated_at = _validation(catalog)
        items.append(CatalogItem(
            id=model.get("id", ""),
            name=model.get("name", ""),
            kind="model",
            category=_category(model.get("name", ""), "model"),
            risk=_risk(model.get("id", ""), "model", None, catalog, model.get("description", "")),
            dependency_health="warning" if missing else "healthy",
            attachment_count=len(meta.get("toolIds", []) or [])
            + len(meta.get("skillIds", []) or [])
            + len(meta.get("knowledge", []) or []),
            version=_version(meta, catalog),
            validation_status=validation_status,
            last_validated_at=last_validated_at,
            details=f"Missing attachments: {', '.join(missing)}" if missing else "All configured attachments resolve.",
        ))

    for tool in tools:
        meta = tool.get("meta") or {}
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        risk = _risk(tool.get("id", ""), "tool", tool.get("specs"), catalog, tool.get("description", ""))
        items.append(CatalogItem(
            id=tool.get("id", ""),
            name=tool.get("name", ""),
            kind="tool",
            category=_category(tool.get("name", ""), "tool"),
            risk=risk,
            dependency_health=str(catalog.get("dependency_health", "unknown")),
            attachment_count=attachment_counts[tool.get("id", "")],
            version=_version(meta, catalog),
            validation_status=validation_status,
            last_validated_at=last_validated_at,
            details=str(catalog.get("details", "Dependency health has not been validated.")),
        ))

    for func in functions:
        meta = func.get("meta") or {}
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        risk = _risk(func.get("id", ""), "function", None, catalog, func.get("description", ""))
        items.append(CatalogItem(
            id=func.get("id", ""),
            name=func.get("name", ""),
            kind="function",
            category=_category(func.get("name", ""), "function"),
            risk=risk,
            dependency_health="healthy" if func.get("is_active") else "warning",
            attachment_count=attachment_counts[func.get("id", "")],
            version=_version(meta, catalog),
            validation_status=validation_status,
            last_validated_at=last_validated_at,
            details="Function is active." if func.get("is_active") else "Function is disabled.",
        ))

    for skill in skills:
        meta = skill.get("meta") or {}
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        items.append(CatalogItem(
            id=skill.get("id", ""),
            name=skill.get("name", ""),
            kind="skill",
            category=_category(skill.get("name", ""), "skill"),
            risk=_risk(skill.get("id", ""), "skill", None, catalog, skill.get("description", "")),
            dependency_health="healthy" if skill.get("is_active") else "warning",
            attachment_count=attachment_counts[skill.get("id", "")],
            version=_version(meta, catalog),
            validation_status=validation_status,
            last_validated_at=last_validated_at,
            details="Skill is active." if skill.get("is_active") else "Skill is disabled.",
        ))

    for prompt in prompts:
        meta = prompt.get("meta") or {}
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        items.append(CatalogItem(
            id=prompt.get("command", prompt.get("id", "")),
            name=prompt.get("name", ""),
            kind="prompt",
            category=_category(prompt.get("name", ""), "prompt"),
            risk=_risk(prompt.get("id", ""), "prompt", None, catalog, prompt.get("description", "")),
            dependency_health="healthy",
            attachment_count=0,
            version=_version(meta, catalog),
            validation_status=validation_status,
            last_validated_at=last_validated_at,
            details="Prompt is active.",
        ))

    for kb in knowledge:
        meta = kb.get("meta") or {}
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        items.append(CatalogItem(
            id=kb.get("id", ""),
            name=kb.get("name", ""),
            kind="knowledge",
            category=_category(kb.get("name", ""), "knowledge"),
            risk="read-only",
            dependency_health="healthy",
            attachment_count=attachment_counts[kb.get("id", "")],
            version=_version(meta, catalog),
            validation_status=validation_status,
            last_validated_at=last_validated_at,
            details="Knowledge base is active.",
        ))

    return items


@app.get("/health")
async def health():
    return {"status": "ok", "service": "workspace-catalog"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "9080")), reload=False)
