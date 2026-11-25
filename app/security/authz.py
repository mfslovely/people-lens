"""Deterministic authorization. Every data access and action goes through here, independent of the LLM.

Scope model:
  employee -> self only
  manager  -> self + full reporting subtree (direct and indirect)
  hr_admin -> whole organisation (read). Actions are still limited to requests assigned to them.
"""

from __future__ import annotations

import re

from psycopg import Connection

from app.agent.models import Principal, Role


class AuthzError(Exception):
    """Raised when the principal may not perform the operation. The message is safe to show to the user."""


def visible_ids(conn: Connection, p: Principal) -> set[int] | None:
    """None means unrestricted (HR admin)."""
    if p.role == Role.HR_ADMIN:
        return None
    ids = {p.employee_id}
    if p.role == Role.MANAGER:
        ids |= {r["employee_id"] for r in conn.execute(
            "SELECT employee_id FROM hr.v_reporting_tree WHERE manager_id = %s", (p.employee_id,)).fetchall()}
    return ids


def can_view(conn: Connection, p: Principal, employee_id: int) -> bool:
    ids = visible_ids(conn, p)
    return ids is None or employee_id in ids


def require_view(conn: Connection, p: Principal, employee_id: int) -> None:
    if not can_view(conn, p, employee_id):
        raise AuthzError("You are not authorized to view this employee's information. "
                         "Access is limited to your own data" + (" and your reporting team." if p.role == Role.MANAGER else "."))


def require_role(p: Principal, *roles: Role) -> None:
    if p.role not in roles:
        raise AuthzError(f"This operation is not available to the {p.role.value} role.")


def resolve_employee(conn: Connection, p: Principal, ref: str | int | None) -> dict:
    """Resolve 'me', an id, an emp_code or a name to one employee the caller may see.

    A name that matches nobody visible gives the same denial whether or not the person exists, so existence is never leaked.
    """
    if ref is None or (isinstance(ref, str) and ref.strip().lower() in {"", "me", "myself", "self", "i"}):
        row = conn.execute("SELECT * FROM hr.employees WHERE id = %s", (p.employee_id,)).fetchone()
        assert row is not None
        return row
    ref_s = str(ref).strip()
    if re.fullmatch(r"\d+", ref_s):
        rows = conn.execute("SELECT * FROM hr.employees WHERE id = %s", (int(ref_s),)).fetchall()
    elif re.fullmatch(r"(?i)emp\d{4}", ref_s):
        rows = conn.execute("SELECT * FROM hr.employees WHERE emp_code = upper(%s)", (ref_s,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM hr.employees WHERE full_name ILIKE %s ORDER BY id", (f"%{ref_s}%",)).fetchall()
    ids = visible_ids(conn, p)
    visible = [r for r in rows if ids is None or r["id"] in ids]
    if not visible:
        raise AuthzError(f"No employee matching '{ref_s}' is within your access scope.")
    if len(visible) > 1:
        exact = [r for r in visible if r["full_name"].lower() == ref_s.lower()]
        if len(exact) == 1:
            return exact[0]
        names = ", ".join(f"{r['full_name']} ({r['emp_code']})" for r in visible[:6])
        raise ValueError(f"'{ref_s}' matches several employees: {names}. Please specify which one.")
    return visible[0]
