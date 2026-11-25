"""Session memory, stored in Postgres (agent.sessions / messages / session_state).

* Short-term history: the last N user/assistant turns, as text only. Raw tool outputs and retrieved documents are not
  replayed, which keeps prompts small and stops retrieved text from coming back as conversation.
* Working memory: `session_state.entities`. Structured references from previous turns (last request ids, last date
  range, last filter) are what let the agent resolve follow-ups like "which of these are next week?".
* Isolation: every read checks that the session belongs to the authenticated user. Entities are only pointers; tools
  re-check permissions on every use, so stale memory can never widen access.
"""

from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

from app.agent.models import Principal
from app.db import tx


class SessionNotFound(Exception):
    pass


def create_session(user_id: int, role: str) -> str:
    with tx() as conn:
        return str(conn.execute("INSERT INTO agent.sessions (user_id, role_at_start) VALUES (%s,%s) RETURNING id",
                                (user_id, role)).fetchone()["id"])


def assert_owner(session_id: str, user_id: int) -> None:
    try:
        sid = uuid.UUID(str(session_id))
    except ValueError as e:
        raise SessionNotFound() from e
    with tx() as conn:
        row = conn.execute("SELECT user_id FROM agent.sessions WHERE id=%s AND status='active'", (sid,)).fetchone()
    if row is None or row["user_id"] != user_id:
        raise SessionNotFound()


def history(session_id: str, turns: int) -> list[dict]:
    with tx() as conn:
        rows = conn.execute(
            """SELECT role, content FROM (SELECT * FROM agent.messages WHERE session_id=%s AND role IN ('user','assistant')
                 ORDER BY id DESC LIMIT %s) x ORDER BY id""", (session_id, turns * 2)).fetchall()
    return [{"role": r["role"], "content": r["content"][:2000]} for r in rows]


def entities(session_id: str) -> dict:
    with tx() as conn:
        row = conn.execute("SELECT entities FROM agent.session_state WHERE session_id=%s", (session_id,)).fetchone()
    return row["entities"] if row else {}


def save_turn(p: Principal, user_msg: str, answer: str, citations: list, trace_id: str, new_entities: dict) -> None:
    sid = str(p.session_id)
    with tx() as conn:
        turn = conn.execute("SELECT coalesce(max(turn),0)+1 AS t FROM agent.messages WHERE session_id=%s", (sid,)).fetchone()["t"]
        conn.execute("INSERT INTO agent.messages (session_id, turn, role, content, trace_id) VALUES (%s,%s,'user',%s,%s)",
                     (sid, turn, user_msg, trace_id))
        conn.execute("INSERT INTO agent.messages (session_id, turn, role, content, citations, trace_id) VALUES (%s,%s,'assistant',%s,%s,%s)",
                     (sid, turn, answer, Jsonb(citations), trace_id))
        conn.execute(
            """INSERT INTO agent.session_state (session_id, entities) VALUES (%s,%s)
               ON CONFLICT (session_id) DO UPDATE SET entities = agent.session_state.entities || EXCLUDED.entities, updated_at=now()""",
            (sid, Jsonb(new_entities)))
        conn.execute("UPDATE agent.sessions SET last_active_at=now() WHERE id=%s", (sid,))


def messages(session_id: str) -> list[dict]:
    with tx() as conn:
        return list(conn.execute("SELECT turn, role, content, citations, trace_id, created_at FROM agent.messages "
                                 "WHERE session_id=%s ORDER BY id", (session_id,)).fetchall())
