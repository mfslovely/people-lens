"""Daily check-in and check-out (Attendance Policy v1.0; WFH Policy v2.1).

Rules enforced here: working days only; no check-in while on approved leave; probation employees cannot mark WFH;
check-in is idempotent (a second check-in returns the existing record); check-out requires a check-in;
arrival after 10:15 is flagged as late.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Literal

from app.agent.models import Principal
from app.config import get_settings
from app.core.dates import is_working_day
from app.core.leave_rules import PROBATION_DAYS, load_holidays
from app.db import tx

LATE_AFTER = time(10, 15)


def _row_out(row: dict | None, today) -> dict:
    if not row:
        return {"date": today, "status": "not_checked_in", "check_in": None, "check_out": None}
    return {"date": today, "status": row["status"], "check_in": row["check_in"].strftime("%H:%M") if row["check_in"] else None,
            "check_out": row["check_out"].strftime("%H:%M") if row["check_out"] else None,
            "late": bool(row["check_in"] and row["check_in"] > LATE_AFTER)}


def today_status(p: Principal) -> dict:
    today = get_settings().today
    with tx() as conn:
        row = conn.execute("SELECT * FROM hr.attendance WHERE employee_id=%s AND date=%s", (p.employee_id, today)).fetchone()
        leave = conn.execute("""SELECT id, leave_type FROM hr.leave_requests WHERE employee_id=%s AND status='approved'
                                AND %s BETWEEN start_date AND end_date""", (p.employee_id, today)).fetchone()
        working = is_working_day(today, load_holidays(conn))
    out = _row_out(row, today)
    out["working_day"] = working
    if leave:
        out["on_leave"] = f"{leave['leave_type']} ({leave['id']})"
    return out


def check_in(p: Principal, mode: Literal["office", "wfh"] = "office", now: datetime | None = None) -> dict:
    s = get_settings()
    today, now = s.today, now or datetime.now()
    with tx() as conn:
        if not is_working_day(today, load_holidays(conn)):
            raise ValueError("Today is not a working day (weekend or public holiday), so no check-in is needed.")
        if conn.execute("""SELECT 1 FROM hr.leave_requests WHERE employee_id=%s AND status='approved'
                           AND %s BETWEEN start_date AND end_date""", (p.employee_id, today)).fetchone():
            raise ValueError("You are on approved leave today, so you can't check in.")
        existing = conn.execute("SELECT * FROM hr.attendance WHERE employee_id=%s AND date=%s FOR UPDATE",
                                (p.employee_id, today)).fetchone()
        if existing and existing["check_in"]:
            return {**_row_out(existing, today), "already_checked_in": True}
        if mode == "wfh":
            joined = conn.execute("SELECT joining_date FROM hr.employees WHERE id=%s", (p.employee_id,)).fetchone()["joining_date"]
            if (today - joined).days < PROBATION_DAYS:
                raise ValueError("Work from home isn't available during the 90-day probation period (WFH Policy §1).")
        status = "wfh" if mode == "wfh" else "present"
        row = conn.execute(
            """INSERT INTO hr.attendance (employee_id, date, status, check_in) VALUES (%s,%s,%s,%s)
               ON CONFLICT (employee_id, date) DO UPDATE SET status=EXCLUDED.status, check_in=EXCLUDED.check_in
               RETURNING *""", (p.employee_id, today, status, now.time().replace(microsecond=0))).fetchone()
    return {**_row_out(row, today), "already_checked_in": False}


def check_out(p: Principal, now: datetime | None = None) -> dict:
    today, now = get_settings().today, now or datetime.now()
    with tx() as conn:
        row = conn.execute("SELECT * FROM hr.attendance WHERE employee_id=%s AND date=%s FOR UPDATE",
                           (p.employee_id, today)).fetchone()
        if not row or not row["check_in"]:
            raise ValueError("You haven't checked in today.")
        if row["check_out"]:
            return {**_row_out(row, today), "already_checked_out": True}
        row = conn.execute("UPDATE hr.attendance SET check_out=%s WHERE employee_id=%s AND date=%s RETURNING *",
                           (now.time().replace(microsecond=0), p.employee_id, today)).fetchone()
    return {**_row_out(row, today), "already_checked_out": False}
