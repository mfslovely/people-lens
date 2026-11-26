"""Analytics tools: predefined workforce statistics, guarded text-to-SQL, anomaly detection and forecasting.

Every query runs against the *scoped* views with the caller's scope set in the transaction, so a manager's statistics
cover only their reporting tree and HR's cover the organisation. The LLM never sees rows outside that scope.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.models import Role
from app.db import tx
from app.intelligence import anomalies, forecast
from app.security.sql_validator import UnsafeSQL, schema_description, validate_sql
from app.tools.registry import ToolContext, tool

MGR_HR = {Role.MANAGER, Role.HR_ADMIN}


class StatsArgs(BaseModel):
    metric: Literal["headcount", "new_joiners", "pending_requests_by_department", "vacant_positions",
                    "leave_taken_by_type", "on_leave_on_date", "headcount_by_department"] = Field(
        description="headcount: count employees (filters: department, status). new_joiners: joined on/after `since`. "
                    "pending_requests_by_department: pending leave counts per department. vacant_positions: open positions. "
                    "leave_taken_by_type: approved leave days by type since `since`. on_leave_on_date: who is on approved leave on `on_date`. "
                    "headcount_by_department: active headcount per department.")
    department: str | None = None
    status: Literal["active", "on_notice", "inactive", "any"] = "active"
    since: date | None = Field(None, description="Start date for new_joiners / leave_taken_by_type")
    on_date: date | None = None


@tool("workforce_stats",
      "Accurate workforce statistics from live data within your access scope (managers: your reporting tree; HR: whole organisation). "
      "Prefer this over run_sql_analytics when a metric fits.", StatsArgs, roles=MGR_HR)
def workforce_stats(ctx: ToolContext, a: StatsArgs):
    scope = "organisation" if ctx.principal.role == Role.HR_ADMIN else "your reporting tree (and you)"
    with tx(ctx.principal) as conn:
        dept_f = "AND department ILIKE %(dept)s" if a.department else ""
        params = {"dept": a.department, "status": a.status, "since": a.since or (ctx.today - timedelta(days=91)),
                  "on": a.on_date or ctx.today}
        if a.metric == "headcount":
            st = "" if a.status == "any" else "AND status = %(status)s::hr.employee_status"
            n = conn.execute(f"SELECT count(*) AS n FROM hr.v_employees_scoped WHERE true {dept_f} {st}", params).fetchone()["n"]
            return {"metric": "headcount", "department": a.department or "all", "status": a.status, "count": n, "scope": scope}
        if a.metric == "headcount_by_department":
            rows = conn.execute("SELECT department, count(*) AS active_employees FROM hr.v_employees_scoped WHERE status='active' "
                                "GROUP BY 1 ORDER BY 2 DESC").fetchall()
            return {"metric": a.metric, "rows": rows, "scope": scope}
        if a.metric == "new_joiners":
            rows = conn.execute(f"""SELECT full_name, department, designation, joining_date FROM hr.v_employees_scoped
                                    WHERE joining_date >= %(since)s {dept_f} ORDER BY joining_date DESC""", params).fetchall()
            return {"metric": a.metric, "since": params["since"], "count": len(rows), "employees": rows, "scope": scope}
        if a.metric == "pending_requests_by_department":
            rows = conn.execute("""SELECT e.department, count(*) AS pending_requests FROM hr.v_leave_scoped l
                                   JOIN hr.v_employees_scoped e ON e.id=l.employee_id WHERE l.status='pending'
                                   GROUP BY 1 ORDER BY 2 DESC, 1""").fetchall()
            return {"metric": a.metric, "rows": rows, "scope": scope}
        if a.metric == "vacant_positions":
            rows = conn.execute(f"""SELECT id AS position_id, title, department, grade FROM hr.v_positions
                                    WHERE assignment_status='vacant' {dept_f} ORDER BY department, title""", params).fetchall()
            return {"metric": a.metric, "count": len(rows), "positions": rows}
        if a.metric == "leave_taken_by_type":
            rows = conn.execute(f"""SELECT l.leave_type, count(*) AS requests, sum(l.days) AS days FROM hr.v_leave_scoped l
                                    JOIN hr.v_employees_scoped e ON e.id=l.employee_id
                                    WHERE l.status='approved' AND l.start_date >= %(since)s {dept_f.replace('department', 'e.department')}
                                    GROUP BY 1 ORDER BY 1""", params).fetchall()
            return {"metric": a.metric, "since": params["since"], "rows": rows, "scope": scope}
        if a.metric == "on_leave_on_date":
            rows = conn.execute(f"""SELECT e.full_name, e.department, l.leave_type, l.start_date, l.end_date FROM hr.v_leave_scoped l
                                    JOIN hr.v_employees_scoped e ON e.id=l.employee_id WHERE l.status='approved'
                                    AND %(on)s BETWEEN l.start_date AND l.end_date {dept_f.replace('department', 'e.department')}
                                    ORDER BY e.full_name""", params).fetchall()
            return {"metric": a.metric, "date": params["on"], "count": len(rows), "employees": rows, "scope": scope}
    raise ValueError("Unsupported metric")


class EmployeeDirectoryArgs(BaseModel):
    department: str | None = Field(None, description="Optional department name filter")
    status: Literal["active", "on_notice", "inactive", "any"] = "any"
    search: str | None = Field(None, min_length=2, max_length=100,
                               description="Optional employee name, code or designation search")
    limit: int = Field(500, ge=1, le=1000)
    offset: int = Field(0, ge=0)


@tool("get_all_employees",
      "HR-only organization employee directory. Returns complete employee records with department, designation, manager, "
      "employment status and application role. Supports filters and pagination; use total_matching to detect more pages.",
      EmployeeDirectoryArgs, roles={Role.HR_ADMIN})
def get_all_employees(ctx: ToolContext, a: EmployeeDirectoryArgs):
    where, params = ["1=1"], []
    if a.department:
        where.append("d.name ILIKE %s")
        params.append(a.department)
    if a.status != "any":
        where.append("e.status = %s::hr.employee_status")
        params.append(a.status)
    if a.search:
        where.append("(e.full_name ILIKE %s OR e.emp_code ILIKE %s OR e.designation ILIKE %s)")
        q = f"%{a.search}%"
        params.extend([q, q, q])
    predicate = " AND ".join(where)
    with tx() as conn:
        total = conn.execute(
            f"""SELECT count(*) AS n FROM hr.employees e
                  JOIN hr.departments d ON d.id=e.department_id
                  JOIN hr.app_users u ON u.employee_id=e.id WHERE {predicate}""", params).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT e.id AS employee_id, e.emp_code, e.full_name AS name, d.name AS department,
                       e.designation, m.full_name AS manager, e.location, e.joining_date, e.status,
                       u.role, u.is_active AS login_active
                  FROM hr.employees e JOIN hr.departments d ON d.id=e.department_id
                  JOIN hr.app_users u ON u.employee_id=e.id
                  LEFT JOIN hr.employees m ON m.id=e.manager_id
                 WHERE {predicate} ORDER BY e.full_name, e.id LIMIT %s OFFSET %s""",
            (*params, a.limit, a.offset)).fetchall()
    next_offset = a.offset + len(rows)
    return {"scope": "organisation", "total_matching": total, "returned": len(rows),
            "limit": a.limit, "offset": a.offset, "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None, "employees": rows}


class WorkforceSummaryArgs(BaseModel):
    department: str | None = Field(None, description="Optional department name; omit for the whole organization")
    lookback_days: int = Field(30, ge=1, le=365,
                               description="Window for attendance, leave and new-joiner statistics")


@tool("generate_workforce_summary",
      "HR-only deterministic workforce report from live organization data: employment status, department and role headcount, "
      "new joiners, positions, leave, and attendance for a configurable lookback window.",
      WorkforceSummaryArgs, roles={Role.HR_ADMIN})
def generate_workforce_summary(ctx: ToolContext, a: WorkforceSummaryArgs):
    since = ctx.today - timedelta(days=a.lookback_days - 1)
    dept = "AND d.name ILIKE %(dept)s" if a.department else ""
    p = {"dept": a.department, "since": since, "today": ctx.today}
    with tx() as conn:
        employee_status = conn.execute(
            f"""SELECT e.status, count(*) AS employees FROM hr.employees e
                  JOIN hr.departments d ON d.id=e.department_id WHERE true {dept}
                  GROUP BY e.status ORDER BY e.status""", p).fetchall()
        departments = conn.execute(
            f"""SELECT d.name AS department, count(*) FILTER (WHERE e.status='active') AS active_employees,
                       count(*) AS total_employees
                  FROM hr.employees e JOIN hr.departments d ON d.id=e.department_id
                 WHERE true {dept} GROUP BY d.name ORDER BY active_employees DESC, d.name""", p).fetchall()
        roles = conn.execute(
            f"""SELECT u.role, count(*) AS active_users FROM hr.app_users u
                  JOIN hr.employees e ON e.id=u.employee_id JOIN hr.departments d ON d.id=e.department_id
                 WHERE u.is_active AND e.status='active' {dept} GROUP BY u.role ORDER BY u.role""", p).fetchall()
        new_joiners = conn.execute(
            f"""SELECT count(*) AS n FROM hr.employees e JOIN hr.departments d ON d.id=e.department_id
                 WHERE e.joining_date BETWEEN %(since)s AND %(today)s {dept}""", p).fetchone()["n"]
        positions = conn.execute(
            f"""SELECT p.assignment_status, count(*) AS positions FROM hr.positions p
                  JOIN hr.departments d ON d.id=p.department_id WHERE true {dept}
                  GROUP BY p.assignment_status ORDER BY p.assignment_status""", p).fetchall()
        leave = conn.execute(
            f"""SELECT r.status, count(*) AS requests, coalesce(sum(r.days),0) AS days
                  FROM hr.leave_requests r JOIN hr.employees e ON e.id=r.employee_id
                  JOIN hr.departments d ON d.id=e.department_id
                 WHERE r.start_date BETWEEN %(since)s AND %(today)s {dept}
                 GROUP BY r.status ORDER BY r.status""", p).fetchall()
        attendance = conn.execute(
            f"""SELECT a.status, count(*) AS records FROM hr.attendance a
                  JOIN hr.employees e ON e.id=a.employee_id JOIN hr.departments d ON d.id=e.department_id
                 WHERE a.date BETWEEN %(since)s AND %(today)s {dept}
                 GROUP BY a.status ORDER BY a.status""", p).fetchall()
    total = sum(r["employees"] for r in employee_status)
    active = next((r["employees"] for r in employee_status if r["status"] == "active"), 0)
    return {"scope": "organisation", "department": a.department or "all", "as_of": ctx.today,
            "window": {"from": since, "to": ctx.today, "days": a.lookback_days},
            "headline": {"total_employees": total, "active_employees": active, "new_joiners": new_joiners},
            "employees_by_status": employee_status, "departments": departments, "active_users_by_role": roles,
            "positions": positions, "leave_requests": leave, "attendance": attendance}


class ApprovalHistoryArgs(BaseModel):
    request_id: str | None = Field(None, pattern=r"^LR-\d{4}-\d{6}$")
    employee: str | None = Field(None, min_length=1, max_length=100,
                                 description="Optional requester name, employee id or employee code")
    action: Literal["submitted", "approved", "rejected", "escalated", "cancelled", "any"] = "any"
    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(200, ge=1, le=1000)
    offset: int = Field(0, ge=0)


@tool("get_approval_history",
      "HR-only organization approval audit history. Returns submitted, approved, rejected, escalated and cancelled events "
      "with requester, actor, comments, timestamps, trace ids and current request status.",
      ApprovalHistoryArgs, roles={Role.HR_ADMIN})
def get_approval_history(ctx: ToolContext, a: ApprovalHistoryArgs):
    if a.start_date and a.end_date and a.end_date < a.start_date:
        raise ValueError("end_date must be on or after start_date")
    where, params = ["1=1"], []
    if a.request_id:
        where.append("h.request_id=%s")
        params.append(a.request_id)
    if a.employee:
        ref = a.employee.strip()
        if ref.isdigit():
            where.append("r.employee_id=%s")
            params.append(int(ref))
        elif ref.upper().startswith("EMP"):
            where.append("requester.emp_code=upper(%s)")
            params.append(ref)
        else:
            where.append("requester.full_name ILIKE %s")
            params.append(f"%{ref}%")
    if a.action != "any":
        where.append("h.action=%s::hr.approval_action")
        params.append(a.action)
    if a.start_date:
        where.append("h.acted_at::date >= %s")
        params.append(a.start_date)
    if a.end_date:
        where.append("h.acted_at::date <= %s")
        params.append(a.end_date)
    predicate = " AND ".join(where)
    joins = """FROM hr.approval_history h JOIN hr.leave_requests r ON r.id=h.request_id
               JOIN hr.employees requester ON requester.id=r.employee_id
               JOIN hr.employees actor ON actor.id=h.actor_id"""
    with tx() as conn:
        total = conn.execute(f"SELECT count(*) AS n {joins} WHERE {predicate}", params).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT h.id AS history_id, h.request_id, requester.id AS employee_id,
                       requester.full_name AS employee, actor.id AS actor_id, actor.full_name AS actor,
                       h.action, h.comment, h.acted_at, h.trace_id, r.leave_type, r.start_date, r.end_date,
                       r.days, r.status AS current_request_status
                  {joins} WHERE {predicate}
                 ORDER BY h.acted_at DESC, h.id DESC LIMIT %s OFFSET %s""",
            (*params, a.limit, a.offset)).fetchall()
    next_offset = a.offset + len(rows)
    return {"scope": "organisation", "total_matching": total, "returned": len(rows),
            "limit": a.limit, "offset": a.offset, "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None, "events": rows}


class SqlArgs(BaseModel):
    question: str = Field(min_length=5, max_length=500, description="The analytical question in plain English")


_SQL_PROMPT = """You write one PostgreSQL SELECT query to answer an HR analytics question.
Use ONLY these views (already filtered to the user's permitted scope):
{schema}
Today is {today}. Leave types: CL, SL, EL, LOP. Request statuses: pending, approved, rejected, cancelled.
Employee statuses: active, on_notice, inactive. Attendance statuses: present, absent, wfh, on_leave, holiday, half_day.
Return only the SQL, no explanation and no markdown fences. If the question cannot be answered with these views, return: SELECT 'UNANSWERABLE' AS reason"""


@tool("run_sql_analytics",
      "Answer an ad-hoc analytical question that workforce_stats cannot, by generating a validated read-only SQL query over "
      "scoped views (employees, leave requests, balances, attendance, positions). Returns the SQL and rows.", SqlArgs, roles=MGR_HR)
def run_sql_analytics(ctx: ToolContext, a: SqlArgs):
    if ctx.llm is None:
        raise ValueError("Analytics query generation is unavailable right now.")
    reply = ctx.llm.chat([{"role": "system", "content": _SQL_PROMPT.format(schema=schema_description(), today=ctx.today)},
                          {"role": "user", "content": a.question}], tier="fast")
    sql = re.sub(r"^```(?:sql)?|```$", "", (reply.content or "").strip(), flags=re.M).strip()
    try:
        safe = validate_sql(sql)
    except UnsafeSQL as e:
        ctx.trace.audit("unsafe_sql_blocked", {"reason": str(e)})
        raise ValueError(f"Generated query was rejected by the SQL safety validator: {e}") from e
    with tx(ctx.principal) as conn:
        conn.execute("SET LOCAL ROLE pl_readonly")
        conn.execute("SET LOCAL statement_timeout = '3000ms'")
        conn.execute("SET LOCAL transaction_read_only = on")
        rows = conn.execute(safe).fetchall()
    return {"sql": safe, "row_count": len(rows), "rows": rows[:100]}


class AnomalyArgs(BaseModel):
    department: str | None = Field(None, description="Optional department filter")
    lookback_days: int = Field(60, ge=14, le=92)


@tool("detect_anomalies",
      "Statistical anomaly detection over attendance and leave within your scope: unusual absence (robust z-score), leave "
      "concentration weeks, and inconsistent records (attendance vs approved leave, reporting to inactive managers).",
      AnomalyArgs, roles=MGR_HR)
def detect_anomalies(ctx: ToolContext, a: AnomalyArgs):
    with tx(ctx.principal) as conn:
        return anomalies.run(conn, ctx.today, a.lookback_days, a.department)


class ForecastArgs(BaseModel):
    start_date: date
    end_date: date
    department: str | None = Field(None, description="HR: department to forecast. Managers: omitted = reporting tree")


@tool("forecast_availability",
      "Forecast daily availability for a future range. Separates SCHEDULED leave (approved/pending facts) from PREDICTED "
      "unplanned absence (model from attendance history). Includes the model's backtest error against a baseline.",
      ForecastArgs, roles=MGR_HR)
def forecast_availability(ctx: ToolContext, a: ForecastArgs):
    if a.start_date < ctx.today:
        raise ValueError("Forecasts are for future dates. Use team_availability or attendance data for past dates.")
    if (a.end_date - a.start_date).days > 31:
        raise ValueError("Forecast range is limited to 31 days.")
    with tx(ctx.principal) as conn:
        return forecast.run(conn, ctx.today, a.start_date, a.end_date, a.department)
