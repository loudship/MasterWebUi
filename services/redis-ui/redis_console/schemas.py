from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


CheckStatus = Literal["pass", "warning", "fail"]


class DiagnosticCheck(BaseModel):
    category: str
    name: str
    status: CheckStatus
    summary: str
    recommendation: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class DebugRun(BaseModel):
    run_id: str
    status: Literal["running", "pass", "warning", "fail"]
    started_at: float
    completed_at: float | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    checks: list[DiagnosticCheck] = Field(default_factory=list)


class DebugRunList(BaseModel):
    count: int
    runs: list[dict[str, Any]]


class OverviewResponse(BaseModel):
    redis: dict[str, Any]
    warnings: list[str]
    recent_reports: list[dict[str, Any]]
    timestamp: float


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
