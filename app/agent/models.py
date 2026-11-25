"""Core typed models shared by the API, the LangGraph orchestrator, the tools and the eval harness."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------- identity
class Role(StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR_ADMIN = "hr_admin"


class Principal(BaseModel):
    """Authenticated caller. Built only from the verified JWT and never from LLM output."""

    model_config = ConfigDict(frozen=True)

    user_id: int
    employee_id: int
    role: Role
    session_id: UUID | None = None  # set for chat turns; None for dashboard / MCP calls


# ---------------------------------------------------------------- routing
class Intent(StrEnum):
    POLICY = "policy"
    PERSONAL_DATA = "personal_data"
    TEAM_DATA = "team_data"
    ANALYTICS = "analytics"
    ACTION = "action"
    MULTI_SOURCE = "multi_source"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    SMALLTALK = "smalltalk"
    ATTENDANCE = "attendance"


class RouteDecision(BaseModel):
    intents: list[Intent] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    entities: dict[str, Any] = Field(default_factory=dict)  # leave_type, raw dates, person refs
    needs_clarification: str | None = None


class GuardVerdict(BaseModel):
    allowed: bool
    category: Literal["injection", "role_spoof", "exfiltration", "destructive", "secret_request", "ok"]
    reason: str = ""


# ---------------------------------------------------------------- evidence
class Citation(BaseModel):
    ref: str = ""  # label shown in the answer, e.g. [P1]
    chunk_id: int
    doc_key: str
    version: str
    section_path: str
    snippet: str
    score: float


class ToolResult(BaseModel):
    tool: str
    ok: bool
    data: Any = None
    error: str | None = None
    source_ids: list[str] = Field(default_factory=list)  # e.g. ["hr.leave_balances:42:EL:2026"]


# ---------------------------------------------------------------- actions
class LeaveProposal(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    days: Decimal = Field(gt=0)
    half_day: bool = False
    balance_before: Decimal
    balance_after: Decimal
    approver_id: int
    approver_name: str
    policy_citations: list[Citation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_dates(self) -> LeaveProposal:
        if self.end_date < self.start_date:
            raise ValueError("end_date before start_date")
        if self.balance_after < 0:
            raise ValueError("insufficient balance")
        return self


class DecisionPayload(BaseModel):
    request_id: str
    decision: Literal["approve", "reject"]
    comment: str | None = None
    requester_id: int
    requester_name: str


class ProposedAction(BaseModel):
    action_id: UUID
    type: Literal["submit_leave", "approve_request", "reject_request"]
    payload: LeaveProposal | DecisionPayload
    idempotency_key: str
    expires_at: datetime


class ActionOutcome(BaseModel):
    """Returned only after the DB write commits. The agent may claim success only from this object."""

    action_id: UUID
    status: Literal["executed", "already_executed", "cancelled", "expired", "failed"]
    request_id: str | None = None
    request_status: str | None = None
    error: str | None = None


# ---------------------------------------------------------------- reasoning outputs
class ReviewRecommendation(BaseModel):
    """Section 6.5: ranking uses operational factors only, never personal characteristics."""

    request_id: str
    priority: int = Field(ge=1)
    factors: list[str]  # overlap, team coverage %, balance sufficiency, notice period, start date
    evidence: list[ToolResult | Citation] = Field(default_factory=list)


class AgentResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    pending_action: ProposedAction | None = None
    confidence: Literal["grounded", "partial", "abstained"]
    trace_id: str
    trace: dict | None = None  # observability summary (tools, latency, tokens) for the UI/eval


# ---------------------------------------------------------------- LangGraph state
class AgentState(TypedDict, total=False):
    principal: Principal
    trace_id: str
    messages: list[Any]
    guard: GuardVerdict
    route: RouteDecision | None
    tool_results: list[ToolResult]
    citations: list[Citation]
    pending_action: ProposedAction | None
    entities: dict[str, Any]  # resolved references carried across turns
    draft: str | None
    final: AgentResponse | None
    errors: list[str]
