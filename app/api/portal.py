"""Portal API: used by the dashboard UI and by the MCP server.

Every endpoint derives scope from the authenticated user and re-uses the same rules engine, authorization and action
code as the chat agent. There is no second, weaker path to the data.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.agent.models import Role
from app.api.auth import AuthedUser, current_user
from app.config import get_settings
from app.core import dates as dates_mod
from app.core.leave_rules import RULES, check_eligibility, load_holidays
from app.db import tx
from app.rag import retriever
from app.security.authz import AuthzError, require_view
from app.services import attendance, notifications
from app.tools.actions import decide_direct, submit_leave_direct

router = APIRouter()


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except AuthzError as e:
        raise HTTPException(403, str(e)) from e
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


def _year() -> int:
    return get_settings().today.year


# ------------------------------------------------------------------ me
@router.get("/me", tags=["portal"])
def me(user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        row = conn.execute(
            """SELECT e.id AS employee_id, e.emp_code, e.full_name AS name, e.designation, d.name AS department,
                      m.full_name AS manager, e.joining_date, e.location,
                      (SELECT count(*) FROM hr.employees r WHERE r.manager_id = e.id AND r.status='active') AS direct_reports
                 FROM hr.employees e JOIN hr.departments d ON d.id=e.department_id LEFT JOIN hr.employees m ON m.id=e.manager_id
                WHERE e.id=%s""", (user.employee_id,)).fetchone()
    return {**row, "role": user.role, "today": get_settings().today}


def _balances(conn, employee_id: int) -> list[dict]:
    return conn.execute(
        """SELECT b.leave_type, t.name, b.accrued, b.used, b.pending, b.available FROM hr.leave_balances b
           JOIN hr.leave_types t ON t.code=b.leave_type WHERE b.employee_id=%s AND b.year=%s
           ORDER BY array_position(ARRAY['CL','SL','EL'], b.leave_type::text)""", (employee_id, _year())).fetchall()


def _requests(conn, where: str, params: tuple, limit: int = 200) -> list[dict]:
    return conn.execute(
        f"""SELECT r.id, e.full_name AS employee, e.designation, d.name AS department, r.leave_type, r.start_date, r.end_date,
                   r.days, r.reason, r.status, ap.full_name AS approver, r.created_at, r.decided_at,
                   (SELECT h.comment FROM hr.approval_history h WHERE h.request_id=r.id AND h.action IN ('approved','rejected')
                     ORDER BY h.id DESC LIMIT 1) AS decision_note
              FROM hr.leave_requests r JOIN hr.employees e ON e.id=r.employee_id JOIN hr.departments d ON d.id=e.department_id
              JOIN hr.employees ap ON ap.id=r.approver_id
             WHERE {where} ORDER BY r.created_at DESC, r.id DESC LIMIT {int(limit)}""", params).fetchall()


@router.get("/me/balances", tags=["portal"])
def my_balances(user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        return {"year": _year(), "balances": _balances(conn, user.employee_id)}


@router.get("/me/leave-requests", tags=["portal"])
def my_requests(user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        return _requests(conn, "r.employee_id=%s", (user.employee_id,))


# ------------------------------------------------------------------ apply (dashboard form / MCP)
@router.get("/leave-types", tags=["leave"])
def leave_types():
    with tx() as conn:
        types = conn.execute("SELECT code, name, annual_quota FROM hr.leave_types ORDER BY code").fetchall()
    for t in types:
        r = RULES[t["code"]]
        t["rules"] = {"max_consecutive_days": r.max_consecutive, "min_notice_days": r.min_notice_days,
                      "notice_unit": r.notice_unit, "half_day_allowed": r.half_day_allowed,
                      "requires_probation_complete": r.requires_probation_complete}
    return types


class ResolveIn(BaseModel):
    expression: str = Field(min_length=1, max_length=200)


@router.post("/dates/resolve", tags=["leave"])
def resolve_dates(body: ResolveIn, user: AuthedUser = Depends(current_user)):
    r = dates_mod.resolve(body.expression, get_settings().today)
    if r is None:
        raise HTTPException(422, f"Could not understand the date '{body.expression}'. Use e.g. 2026-12-03 or 'next Monday'.")
    with tx() as conn:
        wd = dates_mod.working_days(r.start, r.end, load_holidays(conn))
    return {**r.to_dict(), "working_days": len(wd)}


class LeaveIn(BaseModel):
    leave_type: Literal["CL", "SL", "EL", "LOP"]
    start_date: date
    end_date: date
    half_day: bool = False
    reason: str | None = Field(None, max_length=300)


@router.post("/leave/eligibility", tags=["leave"])
def eligibility(body: LeaveIn, user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        el = check_eligibility(conn, user.employee_id, body.leave_type, body.start_date, body.end_date,
                               get_settings().today, body.half_day)
    return el.to_dict()


@router.post("/leave-requests", tags=["leave"], status_code=201)
def apply_leave(body: LeaveIn, idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=80),
                user: AuthedUser = Depends(current_user)):
    """Submit a leave request after the caller has shown the user a summary and obtained explicit confirmation.

    A repeat call with the same Idempotency-Key returns the original request (`duplicate: true`)."""
    return _guard(submit_leave_direct, user.principal(), body.leave_type, body.start_date, body.end_date,
                  body.half_day, body.reason, idempotency_key, "ui")


# ------------------------------------------------------------------ approvals (manager / any approver)
@router.get("/approvals", tags=["approvals"])
def approvals(user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        rows = conn.execute(
            """SELECT r.id, e.full_name AS employee, e.designation, r.leave_type, r.start_date, r.end_date, r.days, r.reason,
                      r.created_at, b.available AS balance_after
                 FROM hr.leave_requests r JOIN hr.employees e ON e.id=r.employee_id
                 LEFT JOIN hr.leave_balances b ON b.employee_id=r.employee_id AND b.leave_type=r.leave_type
                      AND b.year=extract(year FROM r.start_date)
                WHERE r.approver_id=%s AND r.status='pending' ORDER BY r.start_date, r.created_at""",
            (user.employee_id,)).fetchall()
        hol = load_holidays(conn)
        team = [r["employee_id"] for r in conn.execute(
            "SELECT employee_id FROM hr.v_reporting_tree WHERE manager_id=%s", (user.employee_id,)).fetchall()]
        for r in rows:  # how many teammates are already away on those days
            r["teammates_away"] = conn.execute(
                """SELECT count(DISTINCT employee_id) n FROM hr.leave_requests WHERE employee_id = ANY(%s) AND status='approved'
                   AND daterange(start_date,end_date,'[]') && daterange(%s,%s,'[]')""",
                (team, r["start_date"], r["end_date"])).fetchone()["n"] if team else 0
            r["working_days"] = len(dates_mod.working_days(r["start_date"], r["end_date"], hol))
    return rows


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(None, max_length=300)


@router.post("/leave-requests/{request_id}/decision", tags=["approvals"])
def decide(request_id: str, body: DecisionIn, user: AuthedUser = Depends(current_user)):
    return _guard(decide_direct, user.principal(), request_id, body.decision, body.comment)


# ------------------------------------------------------------------ HR
def _require_hr(user: AuthedUser) -> None:
    if user.role != Role.HR_ADMIN:
        raise HTTPException(403, "HR administrators only")


@router.get("/hr/summary", tags=["hr"])
def hr_summary(user: AuthedUser = Depends(current_user)):
    _require_hr(user)
    today = get_settings().today
    with tx() as conn:
        return conn.execute(
            """SELECT count(*) FILTER (WHERE status='pending') AS pending,
                      count(DISTINCT employee_id) FILTER (WHERE status='approved' AND %(t)s BETWEEN start_date AND end_date) AS on_leave_today,
                      count(*) FILTER (WHERE status='approved' AND date_trunc('month', start_date) = date_trunc('month', %(t)s::date)) AS approved_this_month,
                      (SELECT count(*) FROM hr.employees WHERE status='active') AS headcount
                 FROM hr.leave_requests""", {"t": today}).fetchone()


@router.get("/hr/leave-requests", tags=["hr"])
def hr_requests(user: AuthedUser = Depends(current_user), status: str | None = None, department: str | None = None,
                q: str | None = Query(None, max_length=80), start: date | None = None, end: date | None = None,
                role: Literal["manager", "employee", "hr_admin"] | None = None, limit: int = Query(300, le=1000)):
    """All leave requests across the organisation, managers included."""
    _require_hr(user)
    where, params = ["true"], []
    if status:
        where.append("r.status = %s"); params.append(status)
    if department:
        where.append("d.name = %s"); params.append(department)
    if q:
        where.append("(e.full_name ILIKE %s OR r.id ILIKE %s)"); params += [f"%{q}%", f"%{q}%"]
    if start or end:
        where.append("daterange(r.start_date, r.end_date, '[]') && daterange(%s, %s, '[]')")
        params += [start or date.min, end or date.max]
    if role:
        where.append("EXISTS (SELECT 1 FROM hr.app_users u WHERE u.employee_id=e.id AND u.role=%s)"); params.append(role)
    with tx() as conn:
        return _requests(conn, " AND ".join(where), tuple(params), limit)


@router.get("/employees", tags=["hr"])
def employees(user: AuthedUser = Depends(current_user)):
    """People whose leave the caller may view: HR sees everyone, managers see their reporting tree."""
    with tx() as conn:
        if user.role == Role.HR_ADMIN:
            sql, params = "true", ()
        elif user.role == Role.MANAGER:
            sql, params = "e.id IN (SELECT employee_id FROM hr.v_reporting_tree WHERE manager_id=%s)", (user.employee_id,)
        else:
            sql, params = "e.id = %s", (user.employee_id,)
        return conn.execute(
            f"""SELECT e.id AS employee_id, e.full_name AS name, e.designation, d.name AS department, u.role, e.status
                  FROM hr.employees e JOIN hr.departments d ON d.id=e.department_id JOIN hr.app_users u ON u.employee_id=e.id
                 WHERE {sql} ORDER BY e.full_name""", params).fetchall()


@router.get("/employees/{employee_id}/leave", tags=["hr"])
def employee_leave(employee_id: int, user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        _guard(require_view, conn, user.principal(), employee_id)
        return {"balances": _balances(conn, employee_id), "requests": _requests(conn, "r.employee_id=%s", (employee_id,))}


@router.get("/departments", tags=["hr"])
def departments(user: AuthedUser = Depends(current_user)):
    with tx() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM hr.departments ORDER BY name").fetchall()]


# ------------------------------------------------------------------ notifications
@router.get("/notifications", tags=["notifications"])
def get_notifications(user: AuthedUser = Depends(current_user), limit: int = Query(20, le=100)):
    return notifications.list_for(user.employee_id, limit)


class ReadIn(BaseModel):
    ids: list[int] | None = None


@router.post("/notifications/read", tags=["notifications"])
def read_notifications(body: ReadIn, user: AuthedUser = Depends(current_user)):
    return {"marked": notifications.mark_read(user.employee_id, body.ids)}


# ------------------------------------------------------------------ attendance
@router.get("/attendance/today", tags=["attendance"])
def attendance_today(user: AuthedUser = Depends(current_user)):
    return attendance.today_status(user.principal())


class CheckInIn(BaseModel):
    mode: Literal["office", "wfh"] = "office"


@router.post("/attendance/check-in", tags=["attendance"])
def check_in(body: CheckInIn, user: AuthedUser = Depends(current_user)):
    return _guard(attendance.check_in, user.principal(), body.mode)


@router.post("/attendance/check-out", tags=["attendance"])
def check_out(user: AuthedUser = Depends(current_user)):
    return _guard(attendance.check_out, user.principal())


# ------------------------------------------------------------------ policy
class PolicyIn(BaseModel):
    query: str = Field(min_length=3, max_length=300)


@router.post("/policy/search", tags=["policy"])
def policy_search(body: PolicyIn, user: AuthedUser = Depends(current_user)):
    hits = retriever.search(body.query)
    return [{"document": h.citation.doc_key, "version": h.citation.version, "section": h.citation.section_path,
             "text": h.content, "score": h.citation.score} for h in hits]
