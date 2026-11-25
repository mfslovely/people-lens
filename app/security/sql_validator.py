"""Validation for LLM-generated SQL. Only one read-only SELECT over allow-listed scoped views is accepted.

Defence in depth: even a query that passes this check runs as the `pl_readonly` role (SELECT on scoped views only), in a
read-only transaction with a statement timeout and the caller's scope settings applied.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

ALLOWED_VIEWS = {
    "hr.v_employees_scoped": "id, emp_code, full_name, department, designation, manager_id, location, joining_date, status",
    "hr.v_leave_scoped": "id, employee_id, leave_type, start_date, end_date, days, status, approver_id, created_at, decided_at",
    "hr.v_balances_scoped": "employee_id, leave_type, year, accrued, used, pending, available",
    "hr.v_attendance_scoped": "employee_id, date, status, check_in, check_out",
    "hr.v_positions": "id, title, department, grade, assignment_status",
}
_DENY_FUNCS = ("pg_", "set_config", "current_setting", "dblink", "lo_", "query_to_xml", "copy", "txid", "inet_")
MAX_ROWS = 200


class UnsafeSQL(ValueError):
    pass


def validate_sql(sql: str) -> str:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQL(f"SQL could not be parsed: {e}") from e
    if len(statements) != 1:
        raise UnsafeSQL("Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSQL("Only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command,
                             exp.Merge, exp.TruncateTable, exp.Into)):
            raise UnsafeSQL(f"Statement type {type(node).__name__} is not allowed.")
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        full = f"{t.db}.{t.name}".lower() if t.db else t.name.lower()
        if full in cte_names or t.name.lower() in cte_names:
            continue
        if full not in ALLOWED_VIEWS:
            raise UnsafeSQL(f"Table '{full}' is not allowed. Use only: {', '.join(sorted(ALLOWED_VIEWS))}.")
    for f in tree.find_all(exp.Func):
        name = (f.sql_name() if not isinstance(f, exp.Anonymous) else f.name).lower()
        if name.startswith(_DENY_FUNCS):
            raise UnsafeSQL(f"Function '{name}' is not allowed.")
    if isinstance(tree, exp.Select):
        limit = tree.args.get("limit")
        if limit is None:
            tree = tree.limit(MAX_ROWS)
        else:
            try:
                if int(limit.expression.name) > MAX_ROWS:
                    tree = tree.limit(MAX_ROWS)
            except (ValueError, AttributeError):
                tree = tree.limit(MAX_ROWS)
    return tree.sql(dialect="postgres")


def schema_description() -> str:
    return "\n".join(f"{name}({cols})" for name, cols in ALLOWED_VIEWS.items())
