"""Controlled actions: two phases with an explicit human confirmation in between.

Phase 1 (LLM-callable tools): propose_leave / propose_decision validate everything in code and store a
          `pending_actions` row. Nothing changes in HR data.
Phase 2 (NOT an LLM tool): confirm_action / cancel_action, triggered only by the user (a UI button, POST
          /actions/{id}/confirm, or a bare "yes" reply that the orchestrator handles deterministically).

Guarantees:
  * Idempotent: the pending action row is locked with FOR UPDATE. A repeated confirm returns the original result, and
    the leave insert carries a UNIQUE idempotency_key.
  * Re-validated at execution time, because balance, overlap or assignment may have changed since the proposal.
  * Success is reported only from committed database state (request id and status are read back).
  * Approvers act only on requests assigned to them and never on their own.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.agent.models import ActionOutcome, DecisionPayload, LeaveProposal, Principal, ProposedAction, Role
from app.core.leave_rules import check_eligibility
from app.db import tx
from app.security.authz import AuthzError
from app.tools.registry import ToolContext, jsonable, tool

EXPIRY = timedelta(minutes=15)


def _fingerprint(user_id: int, action_type: str, payload: dict) -> str:
    raw = json.dumps({"u": user_id, "t": action_type, "p": payload}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _store_proposal(ctx: ToolContext, action_type: str, payload: dict, validation: dict) -> ProposedAction:
    """Store a proposal. An identical live proposal is reused, so repeated asks never stack duplicates."""
    fp = _fingerprint(ctx.principal.user_id, action_type, payload)
    with tx() as conn:
        existing = conn.execute(
            """SELECT id, idempotency_key, expires_at FROM agent.pending_actions
                WHERE user_id=%s AND idempotency_key LIKE %s AND status='proposed' AND expires_at > now()""",
            (ctx.principal.user_id, fp + ":%")).fetchone()
        if existing:
            row = existing
        else:
            # supersede other open proposals in this session: only one action awaits confirmation at a time
            conn.execute("UPDATE agent.pending_actions SET status='cancelled' WHERE session_id=%s AND status='proposed'",
                         (str(ctx.principal.session_id),))
            row = conn.execute(
                """INSERT INTO agent.pending_actions (session_id, user_id, action_type, payload, validation, idempotency_key, expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s, now() + %s) RETURNING id, idempotency_key, expires_at""",
                (str(ctx.principal.session_id), ctx.principal.user_id, action_type, Jsonb(payload), Jsonb(validation),
                 f"{fp}:{uuid.uuid4().hex[:8]}", EXPIRY)).fetchone()
    pa = ProposedAction(action_id=row["id"], type=action_type,  # type: ignore[arg-type]
                        payload=LeaveProposal(**payload) if action_type == "submit_leave" else DecisionPayload(**payload),
                        idempotency_key=row["idempotency_key"], expires_at=row["expires_at"])
    ctx.proposed = pa
    ctx.trace.audit("action_proposed", {"action_id": str(pa.action_id), "type": action_type})
    return pa


# ------------------------------------------------------------------ propose: leave
class ProposeLeaveArgs(BaseModel):
    leave_type: Literal["CL", "SL", "EL", "LOP"]
    start_date: date
    end_date: date
    half_day: bool = False
    reason: str | None = Field(None, max_length=300)


@tool("propose_leave",
      "Prepare a leave application for the CURRENT USER. Validates eligibility in code and, if eligible, creates a proposal the "
      "user must confirm. It does NOT submit anything. Tell the user to confirm or cancel.", ProposeLeaveArgs, kind="propose")
def propose_leave(ctx: ToolContext, a: ProposeLeaveArgs):
    with tx() as conn:
        el = check_eligibility(conn, ctx.principal.employee_id, a.leave_type, a.start_date, a.end_date, ctx.today, a.half_day)
    if not el.eligible:
        return {"status": "not_eligible", "violations": el.violations, "details": el.to_dict(),
                "note": "No proposal created. Explain the violations to the user."}
    payload = jsonable(LeaveProposal(
        leave_type=a.leave_type, start_date=a.start_date, end_date=a.end_date, days=el.working_days, half_day=a.half_day,
        balance_before=el.balance_available or Decimal(0), balance_after=el.balance_after or Decimal(0),
        approver_id=el.approver_id or 0, approver_name=el.approver_name or "", warnings=el.warnings).model_dump(mode="json"))
    payload["reason"] = a.reason
    pa = _store_proposal(ctx, "submit_leave", {k: v for k, v in payload.items() if k != "reason"},
                         {"eligibility": el.to_dict(), "reason": a.reason})
    return {"status": "awaiting_user_confirmation", "action_id": str(pa.action_id), "summary": payload,
            "approval_rule": el.approval_reason, "warnings": el.warnings,
            "note": "NOT submitted yet. Ask the user to confirm (Confirm button or reply 'yes') or cancel."}


# ------------------------------------------------------------------ propose: decision
class ProposeDecisionArgs(BaseModel):
    request_id: str = Field(pattern=r"^LR-\d{4}-\d{6}$")
    decision: Literal["approve", "reject"]
    comment: str | None = Field(None, max_length=300)


def _check_decision_allowed(conn, p: Principal, request_id: str, lock: bool = False) -> dict:
    r = conn.execute(
        "SELECT r.*, e.full_name FROM hr.leave_requests r JOIN hr.employees e ON e.id=r.employee_id WHERE r.id=%s"
        + (" FOR UPDATE OF r" if lock else ""), (request_id,)).fetchone()
    if r is None:
        raise LookupError(f"Leave request {request_id} was not found.")
    if r["employee_id"] == p.employee_id:
        raise AuthzError("You cannot approve or reject your own leave request (Approval Policy §4).")
    if r["approver_id"] != p.employee_id:
        raise AuthzError(f"Request {request_id} is not assigned to you, so you cannot act on it (Approval Policy §4).")
    if r["status"] != "pending":
        raise ValueError(f"Request {request_id} is already {r['status']}; only pending requests can be decided.")
    return r


@tool("propose_decision",
      "Prepare an approve/reject decision on a pending leave request ASSIGNED TO YOU. Authorization is checked in code. It creates a "
      "proposal the user must confirm; nothing is decided until then.", ProposeDecisionArgs,
      roles={Role.MANAGER, Role.HR_ADMIN}, kind="propose")
def propose_decision(ctx: ToolContext, a: ProposeDecisionArgs):
    with tx() as conn:
        r = _check_decision_allowed(conn, ctx.principal, a.request_id)
    payload = DecisionPayload(request_id=a.request_id, decision=a.decision, comment=a.comment,
                              requester_id=r["employee_id"], requester_name=r["full_name"]).model_dump(mode="json")
    pa = _store_proposal(ctx, f"{a.decision}_request", payload, {"request_status": r["status"]})
    return {"status": "awaiting_user_confirmation", "action_id": str(pa.action_id),
            "summary": {**payload, "leave_type": r["leave_type"], "start_date": r["start_date"], "end_date": r["end_date"],
                        "days": r["days"]},
            "note": "NOT decided yet. Ask the user to confirm or cancel."}


# ------------------------------------------------------------------ phase 2: execute / cancel
def confirm_action(principal: Principal, action_id: str, trace_id: str | None = None) -> ActionOutcome:
    aid = uuid.UUID(str(action_id))
    with tx() as conn:
        pa = conn.execute("SELECT * FROM agent.pending_actions WHERE id=%s FOR UPDATE", (aid,)).fetchone()
        if pa is None or pa["user_id"] != principal.user_id:
            # same answer whether the action is missing or belongs to someone else
            return ActionOutcome(action_id=aid, status="failed", error="Action not found.")
        if pa["status"] == "executed":
            st = conn.execute("SELECT status FROM hr.leave_requests WHERE id=%s", (pa["result_ref"],)).fetchone()
            return ActionOutcome(action_id=aid, status="already_executed", request_id=pa["result_ref"],
                                 request_status=st["status"] if st else None)
        if pa["status"] in ("cancelled", "expired", "failed"):
            return ActionOutcome(action_id=aid, status=pa["status"], error=f"This action was {pa['status']} and cannot be executed.")
        if pa["expires_at"] < datetime.now(timezone.utc):
            conn.execute("UPDATE agent.pending_actions SET status='expired' WHERE id=%s", (aid,))
            return ActionOutcome(action_id=aid, status="expired", error="The proposal expired. Please ask again.")

        try:
            if pa["action_type"] == "submit_leave":
                rid, rstatus = _execute_leave(conn, principal, pa, trace_id)
            else:
                rid, rstatus = _execute_decision(conn, principal, pa, trace_id)
        except (AuthzError, ValueError, LookupError) as e:
            conn.execute("UPDATE agent.pending_actions SET status='failed', error=%s WHERE id=%s", (str(e), aid))
            return ActionOutcome(action_id=aid, status="failed", error=str(e))
        conn.execute("UPDATE agent.pending_actions SET status='executed', result_ref=%s, executed_at=now() WHERE id=%s",
                     (rid, aid))
    return ActionOutcome(action_id=aid, status="executed", request_id=rid, request_status=rstatus)


def _execute_leave(conn, p: Principal, pa: dict, trace_id: str | None) -> tuple[str, str]:
    pl = pa["payload"]
    out = create_leave_request(conn, p, pl["leave_type"], date.fromisoformat(pl["start_date"]), date.fromisoformat(pl["end_date"]),
                               pl.get("half_day", False), (pa["validation"] or {}).get("reason"), pa["idempotency_key"],
                               "agent", trace_id)
    return out["request_id"], out["status"]


def _execute_decision(conn, p: Principal, pa: dict, trace_id: str | None) -> tuple[str, str]:
    pl = pa["payload"]
    out = apply_decision(conn, p, pl["request_id"], pl["decision"], pl.get("comment"), trace_id)
    return out["request_id"], out["status"]


# ------------------------------------------------------------------ shared core (chat confirm, dashboard, MCP)
def create_leave_request(conn, p: Principal, leave_type: str, start: date, end: date, half_day: bool, reason: str | None,
                         idempotency_key: str, channel: str, trace_id: str | None = None) -> dict:
    """Validate and insert one leave request, reserve the balance, and notify the approver and the requester.

    Idempotent on `idempotency_key`: a replay returns the request already created, with `duplicate=True`.
    """
    from app.config import get_settings
    from app.services.notifications import fmt_range, notify

    existing = conn.execute("SELECT id, status, employee_id FROM hr.leave_requests WHERE idempotency_key=%s",
                            (idempotency_key,)).fetchone()
    if existing:
        if existing["employee_id"] != p.employee_id:
            raise AuthzError("Idempotency key belongs to another user.")
        return {"request_id": existing["id"], "status": existing["status"], "duplicate": True}
    el = check_eligibility(conn, p.employee_id, leave_type, start, end, get_settings().today, half_day)
    if not el.eligible:
        raise ValueError("Not eligible: " + "; ".join(el.violations))
    row = conn.execute(
        """INSERT INTO hr.leave_requests (employee_id, leave_type, start_date, end_date, days, half_day, reason, approver_id,
               idempotency_key, created_via) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (idempotency_key) DO NOTHING RETURNING id, status""",
        (p.employee_id, leave_type, start, end, el.working_days, half_day, reason, el.approver_id, idempotency_key,
         "agent" if channel == "agent" else "ui")).fetchone()
    if row is None:  # executed concurrently: return the existing row
        row = conn.execute("SELECT id, status FROM hr.leave_requests WHERE idempotency_key=%s", (idempotency_key,)).fetchone()
        return {"request_id": row["id"], "status": row["status"], "duplicate": True}
    if leave_type != "LOP":
        conn.execute("UPDATE hr.leave_balances SET pending = pending + %s WHERE employee_id=%s AND leave_type=%s AND year=%s",
                     (el.working_days, p.employee_id, leave_type, start.year))
    conn.execute("INSERT INTO hr.approval_history (request_id, actor_id, action, trace_id) VALUES (%s,%s,'submitted',%s)",
                 (row["id"], p.employee_id, trace_id))
    me = conn.execute("SELECT full_name FROM hr.employees WHERE id=%s", (p.employee_id,)).fetchone()["full_name"]
    days = f"{el.working_days.normalize():f} day{'s' if el.working_days != 1 else ''}"
    notify(conn, el.approver_id, "approval_needed", f"Leave request from {me}",
           f"{leave_type} · {fmt_range(start, end)} · {days}", row["id"])
    notify(conn, p.employee_id, "leave_submitted", f"Leave request {row['id']} submitted",
           f"{leave_type} · {fmt_range(start, end)} · awaiting {el.approver_name}", row["id"])
    final = conn.execute("SELECT id, status FROM hr.leave_requests WHERE id=%s", (row["id"],)).fetchone()
    return {"request_id": final["id"], "status": final["status"], "duplicate": False, "approver": el.approver_name,
            "days": float(el.working_days), "balance_after": float(el.balance_after) if el.balance_after is not None else None}


def apply_decision(conn, p: Principal, request_id: str, decision: str, comment: str | None,
                   trace_id: str | None = None) -> dict:
    from app.services.notifications import fmt_range, notify

    r = _check_decision_allowed(conn, p, request_id, lock=True)   # re-checked under a row lock
    new = "approved" if decision == "approve" else "rejected"
    conn.execute("UPDATE hr.leave_requests SET status=%s, decided_at=now() WHERE id=%s", (new, r["id"]))
    if r["leave_type"] != "LOP":
        conn.execute(
            """UPDATE hr.leave_balances SET pending = greatest(pending - %s, 0), used = used + %s
                WHERE employee_id=%s AND leave_type=%s AND year=%s""",
            (r["days"], r["days"] if new == "approved" else 0, r["employee_id"], r["leave_type"], r["start_date"].year))
    conn.execute("INSERT INTO hr.approval_history (request_id, actor_id, action, comment, trace_id) VALUES (%s,%s,%s,%s,%s)",
                 (r["id"], p.employee_id, new, comment, trace_id))
    me = conn.execute("SELECT full_name FROM hr.employees WHERE id=%s", (p.employee_id,)).fetchone()["full_name"]
    notify(conn, r["employee_id"], f"leave_{new}", f"Leave {new}: {r['id']}",
           f"{r['leave_type']} · {fmt_range(r['start_date'], r['end_date'])} · by {me}" + (f" — “{comment}”" if comment else ""),
           r["id"])
    conn.execute("UPDATE hr.notifications SET is_read=true WHERE recipient_id=%s AND request_id=%s AND kind='approval_needed'",
                 (p.employee_id, r["id"]))
    final = conn.execute("SELECT id, status FROM hr.leave_requests WHERE id=%s", (r["id"],)).fetchone()
    return {"request_id": final["id"], "status": final["status"]}


def submit_leave_direct(p: Principal, leave_type: str, start: date, end: date, half_day: bool, reason: str | None,
                        idempotency_key: str, channel: str = "ui") -> dict:
    """Direct submission for callers that collected an explicit confirmation themselves (dashboard form, MCP elicitation)."""
    with tx() as conn:
        return create_leave_request(conn, p, leave_type.upper(), start, end, half_day, reason,
                                    f"{channel}:{p.user_id}:{idempotency_key}", channel)


def decide_direct(p: Principal, request_id: str, decision: Literal["approve", "reject"], comment: str | None = None) -> dict:
    """Approve or reject from the dashboard. The click is the human confirmation; the same checks apply."""
    with tx() as conn:
        return apply_decision(conn, p, request_id, decision, comment)


def cancel_action(principal: Principal, action_id: str) -> ActionOutcome:
    aid = uuid.UUID(str(action_id))
    with tx() as conn:
        pa = conn.execute("SELECT * FROM agent.pending_actions WHERE id=%s FOR UPDATE", (aid,)).fetchone()
        if pa is None or pa["user_id"] != principal.user_id:
            return ActionOutcome(action_id=aid, status="failed", error="Action not found.")
        if pa["status"] != "proposed":
            return ActionOutcome(action_id=aid, status="failed", error=f"Action is already {pa['status']}.")
        conn.execute("UPDATE agent.pending_actions SET status='cancelled' WHERE id=%s", (aid,))
    return ActionOutcome(action_id=aid, status="cancelled")


def open_proposal(principal: Principal) -> dict | None:
    with tx() as conn:
        return conn.execute(
            """SELECT * FROM agent.pending_actions WHERE session_id=%s AND user_id=%s AND status='proposed' AND expires_at > now()
                ORDER BY created_at DESC LIMIT 1""", (str(principal.session_id), principal.user_id)).fetchone()
