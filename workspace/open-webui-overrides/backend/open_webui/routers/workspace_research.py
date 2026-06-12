from __future__ import annotations

import io
import os
import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from open_webui.internal.db import get_async_session
from open_webui.models.knowledge import KnowledgeForm, Knowledges
from open_webui.routers.files import upload_file_handler
from open_webui.utils.auth import get_admin_user
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

router = APIRouter()
RESEARCH_URL = os.getenv("WORKSPACE_RESEARCH_URL", "http://deep-web-mcp:8000/research")
RESEARCH_KNOWLEDGE_NAME = "Knowledge - Research - Web Search Reports"


class DomainFilter(BaseModel):
    domain: str = Field(..., min_length=1)
    mode: Literal["include", "exclude"] = "include"


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    strategy: Literal["auto", "general", "deep"] = "auto"
    domain_filters: list[DomainFilter] = Field(default_factory=list)
    max_iterations: int = Field(default=3, ge=1, le=3)
    max_sources: int = Field(default=8, ge=1, le=8)
    persist: bool = True


async def _research_knowledge(user_id: str, db: AsyncSession):
    items = await Knowledges.get_knowledge_bases(db=db)
    existing = next((item for item in items if item.name == RESEARCH_KNOWLEDGE_NAME), None)
    if existing:
        return existing
    return await Knowledges.insert_new_knowledge(
        user_id,
        KnowledgeForm(
            name=RESEARCH_KNOWLEDGE_NAME,
            description="Complete Markdown artifacts produced by the deterministic web research router.",
            access_grants=[],
        ),
        db=db,
    )


@router.post("")
async def run_workspace_research(
    request: Request,
    form_data: ResearchRequest,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=240) as client:
            response = await client.post(
                RESEARCH_URL,
                json=form_data.model_dump(exclude={"persist"}),
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"Research service failure: {type(exc).__name__}: {exc}") from exc

    if form_data.persist and result.get("status") == "success":
        knowledge = await _research_knowledge(user.id, db)
        if not knowledge:
            raise HTTPException(status_code=500, detail="Could not create research knowledge collection.")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"web-research-{stamp}.md"
        report = result.get("markdown_report", "")
        uploaded = await upload_file_handler(
            request,
            file=UploadFile(file=io.BytesIO(report.encode("utf-8")), filename=filename),
            metadata={"knowledge_id": knowledge.id, "research_query": form_data.query},
            process=True,
            process_in_background=False,
            user=user,
            background_tasks=None,
            db=db,
        )
        result["artifact"] = {
            "knowledge_id": knowledge.id,
            "knowledge_name": knowledge.name,
            "file_id": uploaded["id"] if isinstance(uploaded, dict) else uploaded.id,
            "filename": filename,
        }
    return result
