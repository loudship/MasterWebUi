from __future__ import annotations

import re
from collections import Counter
from typing import Any, Literal

from fastapi import APIRouter, Depends
from open_webui.internal.db import get_async_session
from open_webui.models.knowledge import Knowledges
from open_webui.models.models import Models
from open_webui.models.prompts import Prompts
from open_webui.models.skills import Skills
from open_webui.models.tools import Tools
from open_webui.utils.auth import get_admin_user
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

CatalogKind = Literal["model", "knowledge", "prompt", "skill", "tool"]
RiskLevel = Literal["read-only", "state-changing", "external-network", "operator-only"]
HealthLevel = Literal["healthy", "warning", "unknown"]
ValidationLevel = Literal["passed", "warning", "failed", "not-validated"]

_OPERATOR_IDS = {"run_code_py", "swarm_controls", "deep_web_advanced_tools", "mcp_app_bridge"}
_EXTERNAL_IDS = {"youtube_transcript_provider"}
_STATEFUL_WORDS = re.compile(r"(create|delete|update|write|call_service|start|evict|invoke|run_)")


class CatalogStatusItem(BaseModel):
    id: str
    name: str
    kind: CatalogKind
    category: str
    risk: RiskLevel
    dependency_health: HealthLevel
    attachment_count: int = 0
    version: str | None = None
    validation_status: ValidationLevel = "not-validated"
    last_validated_at: int | None = None
    details: str = ""


class CatalogStatusResponse(BaseModel):
    items: list[CatalogStatusItem]
    counts: dict[str, int] = Field(default_factory=dict)


def _catalog_meta(meta: Any) -> dict[str, Any]:
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump()
    if not isinstance(meta, dict):
        return {}
    manifest = meta.get("manifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("catalog"), dict):
        return manifest["catalog"]
    catalog = meta.get("catalog")
    return catalog if isinstance(catalog, dict) else {}


def _category(name: str, kind: CatalogKind) -> str:
    parts = [part.strip() for part in (name or "").split(" - ") if part.strip()]
    if len(parts) > 1 and parts[0].lower() in {"preset", "tool", "skill", "prompt", "knowledge"}:
        return parts[1]
    return kind.replace("-", " ").title()


def _risk(item_id: str, kind: CatalogKind, specs: list[dict] | None, catalog: dict) -> RiskLevel:
    configured = catalog.get("risk")
    if configured in {"read-only", "state-changing", "external-network", "operator-only"}:
        return configured
    if item_id in _OPERATOR_IDS:
        return "operator-only"
    if item_id in _EXTERNAL_IDS:
        return "external-network"
    if kind != "tool":
        return "read-only"
    names = " ".join(str(spec.get("name", "")) for spec in (specs or []))
    return "state-changing" if _STATEFUL_WORDS.search(names) else "read-only"


def _validation(catalog: dict) -> tuple[ValidationLevel, int | None]:
    status = str(catalog.get("validation_status", "not-validated")).replace("_", "-")
    if status not in {"passed", "warning", "failed", "not-validated"}:
        status = "not-validated"
    last_validated_at = catalog.get("last_validated_at")
    return status, last_validated_at if isinstance(last_validated_at, int) else None


def _version(meta: Any, catalog: dict) -> str | None:
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump()
    manifest = meta.get("manifest", {}) if isinstance(meta, dict) else {}
    value = catalog.get("version") or (manifest.get("version") if isinstance(manifest, dict) else None)
    return str(value) if value else None


@router.get("/status", response_model=CatalogStatusResponse)
async def get_workspace_catalog_status(
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    models = await Models.get_models(db=db)
    tools = await Tools.get_tools(defer_content=True, db=db)
    skills = await Skills.get_skills(db=db)
    prompts = await Prompts.get_prompts(db=db)
    knowledge_bases = await Knowledges.get_knowledge_bases(db=db)

    attachment_counts: Counter[str] = Counter()
    for model in models:
        meta = model.meta.model_dump() if hasattr(model.meta, "model_dump") else (model.meta or {})
        for key in ("toolIds", "filterIds", "skillIds"):
            for item_id in meta.get(key, []) or []:
                attachment_counts[str(item_id)] += 1
        for item in meta.get("knowledge", []) or []:
            if isinstance(item, dict) and item.get("id"):
                attachment_counts[str(item["id"])] += 1

    existing_tools = {tool.id for tool in tools}
    existing_skills = {skill.id for skill in skills}
    existing_knowledge = {item.id for item in knowledge_bases}
    items: list[CatalogStatusItem] = []

    for model in models:
        meta = model.meta.model_dump() if hasattr(model.meta, "model_dump") else (model.meta or {})
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
        items.append(
            CatalogStatusItem(
                id=model.id,
                name=model.name,
                kind="model",
                category=_category(model.name, "model"),
                risk=_risk(model.id, "model", None, catalog),
                dependency_health="warning" if missing else "healthy",
                attachment_count=len(meta.get("toolIds", []) or [])
                + len(meta.get("skillIds", []) or [])
                + len(meta.get("knowledge", []) or []),
                version=_version(meta, catalog),
                validation_status=validation_status,
                last_validated_at=last_validated_at,
                details=f"Missing attachments: {', '.join(missing)}" if missing else "All configured attachments resolve.",
            )
        )

    for tool in tools:
        meta = tool.meta.model_dump() if hasattr(tool.meta, "model_dump") else (tool.meta or {})
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        risk = _risk(tool.id, "tool", getattr(tool, "specs", None), catalog)
        items.append(
            CatalogStatusItem(
                id=tool.id,
                name=tool.name,
                kind="tool",
                category=_category(tool.name, "tool"),
                risk=risk,
                dependency_health=str(catalog.get("dependency_health", "unknown")),
                attachment_count=attachment_counts[tool.id],
                version=_version(meta, catalog),
                validation_status=validation_status,
                last_validated_at=last_validated_at,
                details=str(catalog.get("details", "Dependency health has not been validated.")),
            )
        )

    for skill in skills:
        meta = skill.meta.model_dump() if hasattr(skill.meta, "model_dump") else (skill.meta or {})
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        items.append(
            CatalogStatusItem(
                id=skill.id,
                name=skill.name,
                kind="skill",
                category=_category(skill.name, "skill"),
                risk=_risk(skill.id, "skill", None, catalog),
                dependency_health="healthy" if skill.is_active else "warning",
                attachment_count=attachment_counts[skill.id],
                version=_version(meta, catalog),
                validation_status=validation_status,
                last_validated_at=last_validated_at,
                details="Skill is active." if skill.is_active else "Skill is disabled.",
            )
        )

    for prompt in prompts:
        meta = prompt.meta or {}
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        items.append(
            CatalogStatusItem(
                id=prompt.id,
                name=prompt.name,
                kind="prompt",
                category=_category(prompt.name, "prompt"),
                risk=_risk(prompt.id, "prompt", None, catalog),
                dependency_health="healthy" if prompt.is_active else "warning",
                attachment_count=0,
                version=_version(meta, catalog),
                validation_status=validation_status,
                last_validated_at=last_validated_at,
                details="Prompt is active." if prompt.is_active else "Prompt is disabled.",
            )
        )

    for knowledge in knowledge_bases:
        meta = knowledge.meta or {}
        catalog = _catalog_meta(meta)
        validation_status, last_validated_at = _validation(catalog)
        file_count = len(await Knowledges.get_files_by_id(knowledge.id, db=db))
        items.append(
            CatalogStatusItem(
                id=knowledge.id,
                name=knowledge.name,
                kind="knowledge",
                category=_category(knowledge.name, "knowledge"),
                risk="read-only",
                dependency_health="healthy" if file_count else "warning",
                attachment_count=attachment_counts[knowledge.id],
                version=_version(meta, catalog),
                validation_status=validation_status,
                last_validated_at=last_validated_at,
                details=f"{file_count} knowledge document(s) available.",
            )
        )

    return CatalogStatusResponse(
        items=items,
        counts=dict(Counter(item.kind for item in items)),
    )
