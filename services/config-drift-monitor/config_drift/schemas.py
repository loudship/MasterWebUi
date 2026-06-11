from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class DiffRecord(BaseModel):
    rule_id: str
    logical_path: str
    label: str
    domain: str
    parent_plane: str
    child_plane: str
    expected: Any = None
    observed: Any = None
    status: Literal["aligned", "override", "drift", "unavailable", "unobservable", "ignored"]
    severity: Literal["critical", "warning", "info"]
    enforced: bool = False
    entity_label: str
    source_endpoint: str
    observed_at: float
    recommendation: str = ""
    detail: str = ""
    provenance: str = ""


class PlaneSummary(BaseModel):
    name: str
    status: str
    observed_at: float | None = None
    latency_ms: int = 0
    item_count: int = 0
    error: str = ""


class OverviewResponse(BaseModel):
    status: Literal["aligned", "warning", "fail"]
    counts: dict[str, int]
    planes: list[PlaneSummary]
    baseline: dict[str, Any]
    generated_at: float
    event_version: int


class SnapshotResponse(BaseModel):
    overview: OverviewResponse
    planes: dict[str, Any]
    diffs: list[DiffRecord]


class DiffListResponse(BaseModel):
    count: int
    diffs: list[DiffRecord]


class RefreshResponse(BaseModel):
    status: str
    generated_at: float
    event_version: int


class HealthResponse(BaseModel):
    status: str
    baseline_valid: bool
    open_webui_configured: bool
    generated_at: float | None = None

