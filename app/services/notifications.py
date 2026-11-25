"""In-app notifications. They are written inside the same transaction as the leave change that triggers them."""

from __future__ import annotations

from psycopg import Connection

from app.db import tx


def notify(conn: Connection, recipient_id: int, kind: str, title: str, body: str | None = None,
           request_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO hr.notifications (recipient_id, kind, title, body, request_id) VALUES (%s,%s,%s,%s,%s)",
        (recipient_id, kind, title, body, request_id))


def fmt_range(start, end) -> str:
    return start.strftime("%d %b") if start == end else f"{start.strftime('%d %b')} – {end.strftime('%d %b')}"


def list_for(employee_id: int, limit: int = 20) -> dict:
    with tx() as conn:
        rows = conn.execute(
            """SELECT id, kind, title, body, request_id, is_read, created_at FROM hr.notifications
                WHERE recipient_id=%s ORDER BY created_at DESC, id DESC LIMIT %s""", (employee_id, limit)).fetchall()
        unread = conn.execute("SELECT count(*) n FROM hr.notifications WHERE recipient_id=%s AND NOT is_read",
                              (employee_id,)).fetchone()["n"]
    return {"unread": unread, "items": rows}


def mark_read(employee_id: int, ids: list[int] | None = None) -> int:
    with tx() as conn:
        if ids:
            cur = conn.execute("UPDATE hr.notifications SET is_read=true WHERE recipient_id=%s AND id = ANY(%s) AND NOT is_read",
                               (employee_id, ids))
        else:
            cur = conn.execute("UPDATE hr.notifications SET is_read=true WHERE recipient_id=%s AND NOT is_read", (employee_id,))
        return cur.rowcount
