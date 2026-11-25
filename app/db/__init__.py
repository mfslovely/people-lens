"""Database access: a single connection pool plus helpers that apply the caller's scope."""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

if TYPE_CHECKING:
    from app.agent.models import Principal

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
        atexit.register(_pool.close)
    return _pool


@contextmanager
def tx(principal: "Principal | None" = None) -> Iterator[Connection]:
    """One transaction. If a principal is given, the scoped views are restricted to what that principal may see."""
    with pool().connection() as conn:
        with conn.transaction():
            if principal is not None:
                apply_scope(conn, principal)
            yield conn


def apply_scope(conn: Connection, principal: "Principal") -> None:
    conn.execute(
        "SELECT set_config('app.employee_id', %s, true), set_config('app.role', %s, true)",
        (str(principal.employee_id), principal.role.value),
    )


def fetch_all(conn: Connection, sql: str, params: Any = None) -> list[dict]:
    return list(conn.execute(sql, params).fetchall())


def fetch_one(conn: Connection, sql: str, params: Any = None) -> dict | None:
    return conn.execute(sql, params).fetchone()
