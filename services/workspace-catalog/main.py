"""
Standalone Workspace Catalog Status Service.

Derives catalog risk, dependency-health, and validation metadata for all
workspace items (models, tools, functions, skills, prompts, knowledge) by
reading from Open WebUI's public REST API.  Runs as a separate container —
no Open WebUI source patching required.

Environment variables
---------------------
OWUI_BASE_URL     Internal URL of the Open WebUI service (default: http://open-webui:8080).
OWUI_ADMIN_TOKEN  Admin API token issued by Open WebUI (required).
PORT              Listen port (default: 9080).
"""
from __future__ import annotations

import os
import re
import logging
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
# Risk classification
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


def _risk(item_id: str, description: str) -> RiskLevel:
    if item_id in _OPERATOR_IDS:
        return "operator-only"
    if item_id in _EXTERNAL_IDS:
        return "external-network"
    if _STATEFUL_WORDS.search(description or ""):
        return "state-changing"
    return "read-only"


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
        tools     = await _get_owui(client, "/api/v1/tools")
        functions = await _get_owui(client, "/api/v1/functions")
        models    = await _get_owui(client, "/api/v1/models")
        skills    = await _get_owui(client, "/api/v1/skills")
        prompts   = await _get_owui(client, "/api/v1/prompts")
        knowledge = await _get_owui(client, "/api/v1/knowledge")

    items: list[CatalogItem] = []

    for tool in tools:
        meta = _catalog_meta(tool.get("meta") or {})
        items.append(CatalogItem(
            id=tool.get("id", ""),
            name=tool.get("name", ""),
            kind="tool",
            category=meta.get("category", "tool"),
            risk=_risk(tool.get("id", ""), tool.get("description", "")),
            validation_status=meta.get("validation_status", "not-validated"),
            last_validated_at=meta.get("last_validated_at"),
            details=meta.get("details", ""),
        ))

    for func in functions:
        meta = _catalog_meta(func.get("meta") or {})
        items.append(CatalogItem(
            id=func.get("id", ""),
            name=func.get("name", ""),
            kind="function",
            category=meta.get("category", "function"),
            risk=_risk(func.get("id", ""), func.get("description", "")),
            validation_status=meta.get("validation_status", "not-validated"),
            last_validated_at=meta.get("last_validated_at"),
            details=meta.get("details", ""),
        ))

    for model in models:
        items.append(CatalogItem(
            id=model.get("id", ""),
            name=model.get("name", ""),
            kind="model",
            category="model",
            risk="read-only",
        ))

    for skill in skills:
        meta = _catalog_meta(skill.get("meta") or {})
        items.append(CatalogItem(
            id=skill.get("id", ""),
            name=skill.get("name", ""),
            kind="skill",
            category=meta.get("category", "skill"),
            risk="read-only",
            details=meta.get("details", ""),
        ))

    for prompt in prompts:
        items.append(CatalogItem(
            id=prompt.get("command", prompt.get("id", "")),
            name=prompt.get("name", ""),
            kind="prompt",
            category="prompt",
            risk="read-only",
        ))

    for kb in knowledge:
        items.append(CatalogItem(
            id=kb.get("id", ""),
            name=kb.get("name", ""),
            kind="knowledge",
            category="knowledge",
            risk="read-only",
        ))

    return items


@app.get("/health")
async def health():
    return {"status": "ok", "service": "workspace-catalog"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "9080")), reload=False)
