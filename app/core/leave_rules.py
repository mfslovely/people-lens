"""Leave business rules, applied in code (outside the LLM).

The numbers mirror data/policies/leave_policy_v2.md and approval_escalation_policy.md.
The tests check that code and policy text agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from psycopg import Connection

from app.core.dates import working_days


@dataclass(frozen=True)
class LeaveRule:
    code: str
    max_consecutive: int | None
    min_notice_days: int          # calendar days (EL) or working days (CL), see notice_unit
    notice_unit: str              # "calendar" | "working"
    retro_days: int               # how many calendar days in the past a request may start
    half_day_allowed: bool
    requires_probation_complete: bool
    policy_section: str


RULES: dict[str, LeaveRule] = {
    "CL": LeaveRule("CL", 3, 1, "working", 0, True, False, "Leave and Holiday Policy v2.0 §2"),
    "SL": LeaveRule("SL", None, 0, "calendar", 7, True, False, "Leave and Holiday Policy v2.0 §3"),
    "EL": LeaveRule("EL", 15, 7, "calendar", 0, False, True, "Leave and Holiday Policy v2.0 §4"),
    "LOP": LeaveRule("LOP", None, 0, "calendar", 0, False, False, "Leave and Holiday Policy v2.0 §5"),
}
PROBATION_DAYS = 90
SKIP_LEVEL_THRESHOLD_DAYS = 5


@dataclass
class Eligibility:
    eligible: bool
    leave_type: str
    start_date: date
    end_date: date
    working_days: Decimal
    balance_available: Decimal | None
    balance_after: Decimal | None
    approver_id: int | None
    approver_name: str | None
    approval_reason: str
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    policy_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "leave_type": self.leave_type,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "working_days_requested": float(self.working_days),
            "balance_available": float(self.balance_available) if self.balance_available is not None else None,
            "balance_after": float(self.balance_after) if self.balance_after is not None else None,
            "approver_id": self.approver_id,
            "approver_name": self.approver_name,
            "approval_rule": self.approval_reason,
            "violations": self.violations,
            "warnings": self.warnings,
            "policy_refs": self.policy_refs,
        }


def load_holidays(conn: Connection) -> set[date]:
    return {r["date"] for r in conn.execute("SELECT date FROM hr.holidays").fetchall()}


def manager_chain(conn: Connection, employee_id: int) -> list[dict]:
    rows = conn.execute(
        """WITH RECURSIVE chain AS (
               SELECT m.id, m.full_name, m.status, m.manager_id, 1 AS lvl
                 FROM hr.employees e JOIN hr.employees m ON m.id = e.manager_id WHERE e.id = %s
               UNION ALL
               SELECT m.id, m.full_name, m.status, m.manager_id, c.lvl + 1
                 FROM chain c JOIN hr.employees m ON m.id = c.manager_id WHERE c.lvl < 10)
           SELECT * FROM chain ORDER BY lvl""",
        (employee_id,),
    ).fetchall()
    return list(rows)


def _on_leave(conn: Connection, employee_id: int, day: date) -> bool:
    return conn.execute(
        """SELECT 1 FROM hr.leave_requests WHERE employee_id = %s AND status = 'approved'
              AND %s BETWEEN start_date AND end_date""",
        (employee_id, day),
    ).fetchone() is not None


def resolve_approver(conn: Connection, employee_id: int, days: Decimal, today: date) -> tuple[dict | None, str]:
    """Approval & Escalation Policy §1–2: manager for ≤5 days, skip-level for >5, skip unavailable approvers."""
    chain = manager_chain(conn, employee_id)
    if not chain:
        return None, "No manager in the reporting chain; HR must route this request."
    if days > SKIP_LEVEL_THRESHOLD_DAYS and len(chain) > 1:
        idx, reason = 1, f"More than {SKIP_LEVEL_THRESHOLD_DAYS} working days requires skip-level approval (Approval Policy §1.2)."
    else:
        idx, reason = 0, f"Up to {SKIP_LEVEL_THRESHOLD_DAYS} working days is approved by the reporting manager (Approval Policy §1.1)."
    for cand in chain[idx:]:
        if cand["status"] == "active" and not _on_leave(conn, cand["id"], today):
            if cand["id"] != chain[idx]["id"]:
                reason += f" {chain[idx]['full_name']} is unavailable, so the request escalates to the next manager (Approval Policy §2)."
            return cand, reason
    return None, "No available approver in the reporting chain; HR must route this request."


def check_eligibility(
    conn: Connection,
    employee_id: int,
    leave_type: str,
    start: date,
    end: date,
    today: date,
    half_day: bool = False,
    exclude_request_id: str | None = None,
) -> Eligibility:
    leave_type = leave_type.upper()
    violations: list[str] = []
    warnings: list[str] = []
    rule = RULES.get(leave_type)
    holidays = load_holidays(conn)

    if rule is None:
        return Eligibility(False, leave_type, start, end, Decimal(0), None, None, None, None, "",
                           [f"Unknown leave type '{leave_type}'. Valid types: CL, SL, EL, LOP."])
    if end < start:
        violations.append("End date is before start date.")

    days_list = working_days(start, end, holidays)
    n_days = Decimal(len(days_list))
    if half_day:
        if not rule.half_day_allowed:
            violations.append(f"Half-day is not permitted for {leave_type}.")
        elif start != end:
            violations.append("A half-day request must be for a single date.")
        n_days = Decimal("0.5") if days_list else Decimal(0)
    if n_days == 0 and not violations:
        violations.append("The selected dates contain no working days (weekends/public holidays are not counted).")

    emp = conn.execute("SELECT id, joining_date, status FROM hr.employees WHERE id = %s", (employee_id,)).fetchone()
    if emp is None:
        violations.append("Employee not found.")
        return Eligibility(False, leave_type, start, end, n_days, None, None, None, None, "", violations)
    if emp["status"] != "active":
        violations.append(f"Employee status is '{emp['status']}'.")

    # notice / retroactivity
    if start < today:
        if (today - start).days > rule.retro_days:
            violations.append(
                f"{leave_type} cannot start in the past" + (f" (more than {rule.retro_days} days ago)." if rule.retro_days else ".")
            )
    elif rule.min_notice_days:
        if rule.notice_unit == "calendar":
            notice = (start - today).days
        else:
            notice = len(working_days(today + timedelta(days=1), start, holidays)) if start > today else 0
        if notice < rule.min_notice_days:
            violations.append(
                f"{leave_type} requires at least {rule.min_notice_days} {rule.notice_unit} day(s) notice; "
                f"this request gives {notice}."
            )

    if rule.max_consecutive and n_days > rule.max_consecutive:
        violations.append(f"{leave_type} is limited to {rule.max_consecutive} consecutive working days per request.")

    if rule.requires_probation_complete and (start - emp["joining_date"]).days < PROBATION_DAYS:
        violations.append(f"{leave_type} is available only after the {PROBATION_DAYS}-day probation period.")

    if leave_type == "SL" and n_days > 2:
        warnings.append("A medical certificate is required for sick leave longer than 2 consecutive working days.")
    if leave_type == "LOP":
        warnings.append("Loss-of-pay leave also requires HR Administrator approval.")

    # balance
    available = after = None
    if leave_type != "LOP":
        bal = conn.execute(
            "SELECT available FROM hr.leave_balances WHERE employee_id=%s AND leave_type=%s AND year=%s",
            (employee_id, leave_type, start.year),
        ).fetchone()
        available = Decimal(bal["available"]) if bal else Decimal(0)
        after = available - n_days
        if after < 0:
            violations.append(f"Insufficient {leave_type} balance: {available} available, {n_days} requested.")

    # overlap
    overlap = conn.execute(
        """SELECT id, leave_type, start_date, end_date, status FROM hr.leave_requests
            WHERE employee_id=%s AND status IN ('pending','approved')
              AND daterange(start_date, end_date, '[]') && daterange(%s, %s, '[]')
              AND (%s::text IS NULL OR id <> %s)""",
        (employee_id, start, end, exclude_request_id, exclude_request_id),
    ).fetchall()
    for o in overlap:
        violations.append(
            f"Overlaps existing {o['status']} request {o['id']} ({o['leave_type']} {o['start_date']} to {o['end_date']})."
        )

    approver, reason = resolve_approver(conn, employee_id, n_days, today)
    if approver is None:
        violations.append(reason)

    return Eligibility(
        eligible=not violations,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        working_days=n_days,
        balance_available=available,
        balance_after=after,
        approver_id=approver["id"] if approver else None,
        approver_name=approver["full_name"] if approver else None,
        approval_reason=reason,
        violations=violations,
        warnings=warnings,
        policy_refs=[rule.policy_section, "Leave Approval and Escalation Policy v1.2 §1"],
    )
