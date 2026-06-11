from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class DiagnosticRunRequest(BaseModel):
    query: str = Field("Open WebUI web search tools", min_length=1, max_length=500)
    extract_url: str = Field("https://example.com", min_length=8, max_length=2048)
    include_monitor: bool = False
    prompt: str = Field(
        "Briefly explain why evidence-backed web research matters.",
        min_length=1,
        max_length=8000,
    )
    expected_text: list[str] = Field(default_factory=list)
    require_citations: bool = False
    require_tool_call: bool = False
    model_id: str | None = None


class PromptVerifyRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    model_id: str | None = None
    expected_text: list[str] = Field(default_factory=list)
    require_citations: bool = False
    require_tool_call: bool = False


class PromptVerifyResponse(BaseModel):
    configured: bool
    passed: bool
    model_id: str
    content: str = ""
    latency_ms: int = 0
    expected_checks: dict[str, bool] = Field(default_factory=dict)
    citations: list[Any] = Field(default_factory=list)
    tool_calls: list[Any] = Field(default_factory=list)
    detail: str = ""


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ServiceProbe(BaseModel):
    name: str
    label: str
    status: str
    detail: str
    latency_ms: int


class ControlOverviewResponse(BaseModel):
    summary: dict[str, Any]
    services: list[ServiceProbe]
    capabilities: dict[str, Any]
    recent_runs: list[dict[str, Any]]
    timestamp: float


class DiagnosticRunRecord(BaseModel):
    run_id: str
    kind: str
    status: str
    started_at: float
    completed_at: float | None = None
    error: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)


class DiagnosticRunListResponse(BaseModel):
    count: int
    runs: list[dict[str, Any]]


class PromptModelsResponse(BaseModel):
    configured: bool
    default_model_id: str
    models: list[dict[str, str]]
    detail: str


RunStatus = Literal["running", "passed", "failed", "partial"]
