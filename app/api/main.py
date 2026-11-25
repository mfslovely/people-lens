"""HTTP API. Interactive docs at /docs.

Auth model (demo): the evaluator picks a seeded identity through /auth/login and receives a signed JWT that holds only
the user id. On every request the role and active status are re-read from hr.app_users, so permissions are re-checked
per request and cannot be forged by the token holder or the conversation.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.agent import memory
from app.api.auth import AuthedUser, current_user, user_row
from app.agent.graph import Orchestrator, action_outcome_text
from app.agent.models import ActionOutcome, AgentResponse, Principal, Role
from app.config import get_settings
from app.core.observability import setup_logging
from app.db import tx
from app.tools.actions import cancel_action, confirm_action

setup_logging()
app = FastAPI(title="People Lens: Workforce AI Assistant", version="1.0.0",
              description="Agentic HR assistant: RAG over policies, scoped data tools, controlled leave actions.")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8501"], allow_methods=["*"], allow_headers=["*"])


@lru_cache
def orchestrator() -> Orchestrator:
    return Orchestrator()


# ------------------------------------------------------------------ auth
class LoginIn(BaseModel):
    employee_id: int


class LoginOut(BaseModel):
    token: str
    employee_id: int
    name: str
    role: Role


@app.post("/auth/login", response_model=LoginOut, tags=["auth"])
def login(body: LoginIn):
    u = user_row(body.employee_id)
    if not u or not u["is_active"]:
        raise HTTPException(401, "Unknown or inactive identity")
    token = jwt.encode({"sub": str(u["user_id"]), "emp": u["employee_id"],
                        "exp": datetime.now(timezone.utc) + timedelta(hours=8)}, get_settings().jwt_secret, algorithm="HS256")
    return LoginOut(token=token, employee_id=u["employee_id"], name=u["full_name"], role=u["role"])


@app.get("/auth/directory", tags=["auth"])
def directory():
    """Active people for the demo sign-in picker."""
    with tx() as conn:
        return conn.execute(
            """SELECT e.id AS employee_id, e.full_name AS name, u.role, e.designation, d.name AS department
                 FROM hr.employees e JOIN hr.app_users u ON u.employee_id=e.id JOIN hr.departments d ON d.id=e.department_id
                WHERE u.is_active ORDER BY e.full_name""").fetchall()


@app.get("/auth/identities", tags=["auth"])
def identities():
    """Demo personas plus a sample of other seeded users."""
    personas = [20, 21, 8, 9, 30, 13, 22]
    with tx() as conn:
        rows = conn.execute(
            """SELECT e.id AS employee_id, e.full_name AS name, u.role, e.designation, d.name AS department
                 FROM hr.employees e JOIN hr.app_users u ON u.employee_id=e.id JOIN hr.departments d ON d.id=e.department_id
                WHERE e.id = ANY(%s) ORDER BY array_position(%s, e.id)""", (personas, personas)).fetchall()
    return rows


def principal_for(user: AuthedUser, session_id: str) -> Principal:
    try:
        memory.assert_owner(session_id, user.user_id)
    except memory.SessionNotFound as e:
        raise HTTPException(404, "Session not found") from e
    return Principal(user_id=user.user_id, employee_id=user.employee_id, role=user.role, session_id=uuid.UUID(session_id))


# ------------------------------------------------------------------ chat
@app.post("/sessions", tags=["chat"])
def new_session(user: AuthedUser = Depends(current_user)):
    return {"session_id": memory.create_session(user.user_id, user.role.value)}


class ChatIn(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=4000)


@app.post("/chat", response_model=AgentResponse, tags=["chat"])
def chat(body: ChatIn, user: AuthedUser = Depends(current_user)):
    return orchestrator().run(principal_for(user, body.session_id), body.message)


@app.get("/sessions/{session_id}/messages", tags=["chat"])
def session_messages(session_id: str, user: AuthedUser = Depends(current_user)):
    principal_for(user, session_id)
    return memory.messages(session_id)


# ------------------------------------------------------------------ actions (human confirmation, not LLM-callable)
class ActionIn(BaseModel):
    session_id: str


@app.post("/actions/{action_id}/confirm", tags=["actions"])
def confirm(action_id: str, body: ActionIn, user: AuthedUser = Depends(current_user)):
    p = principal_for(user, body.session_id)
    with tx() as conn:
        prop = conn.execute("SELECT * FROM agent.pending_actions WHERE id=%s", (action_id,)).fetchone()
    out: ActionOutcome = confirm_action(p, action_id)
    msg = action_outcome_text(prop, out) if prop and prop["user_id"] == user.user_id else "Action not found."
    memory.save_turn(p, "[Confirm button]", msg, [], f"action_{action_id[:8]}", {"last_action": out.model_dump(mode="json")})
    return {"outcome": out, "message": msg}


@app.post("/actions/{action_id}/cancel", tags=["actions"])
def cancel(action_id: str, body: ActionIn, user: AuthedUser = Depends(current_user)):
    p = principal_for(user, body.session_id)
    out = cancel_action(p, action_id)
    msg = "Cancelled. Nothing was submitted." if out.status == "cancelled" else f"Could not cancel: {out.error}"
    memory.save_turn(p, "[Cancel button]", msg, [], f"action_{action_id[:8]}", {})
    return {"outcome": out, "message": msg}


# ------------------------------------------------------------------ ops
@app.get("/health", tags=["ops"])
def health():
    t0 = time.perf_counter()
    try:
        with tx() as conn:
            conn.execute("SELECT 1")
        db = "ok"
    except Exception:  # noqa: BLE001
        db = "down"
    s = get_settings()
    llm = s.llm
    return {"db": db, "llm_provider": llm.provider, "llm_configured": llm.configured, "primary_model": llm.primary_model,
            "fast_model": llm.fast_model, "today": s.today, "latency_ms": int((time.perf_counter() - t0) * 1000)}


@app.get("/traces/{trace_id}", tags=["ops"])
def get_trace(trace_id: str, user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        t = conn.execute("SELECT * FROM ops.traces WHERE trace_id=%s", (trace_id,)).fetchone()
        if not t or (t["user_id"] != user.user_id and user.role != Role.HR_ADMIN):
            raise HTTPException(404, "Trace not found")
        calls = conn.execute("SELECT tool_name, args_redacted, authz_decision, status, latency_ms, retries, error, "
                             "retrieved_chunk_ids FROM ops.tool_calls WHERE trace_id=%s ORDER BY id", (trace_id,)).fetchall()
    return {"trace": t, "tool_calls": calls}


@app.get("/metrics/summary", tags=["ops"])
def metrics(user: AuthedUser = Depends(current_user)):
    if user.role != Role.HR_ADMIN:
        raise HTTPException(403, "HR admin only")
    with tx() as conn:
        return conn.execute(
            """SELECT count(*) AS requests, round(avg(latency_ms)) AS avg_latency_ms,
                      percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
                      sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, sum(cost_usd) AS cost_usd,
                      count(*) FILTER (WHERE status='blocked') AS blocked, count(*) FILTER (WHERE fallback_used) AS fallbacks
                 FROM ops.traces""").fetchone()


from app.api.portal import router as portal_router  # noqa: E402

app.include_router(portal_router)
