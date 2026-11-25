"""MCP server end to end: in-process MCP client → MCP server → FastAPI (ASGI) → Postgres, including elicitation."""

from __future__ import annotations

import json

import anyio
import httpx
import mcp_types as types
import pytest
from mcp import Client

import app.mcp_server as srv
from app.api.main import app
from app.db import tx


def run(coro):
    return anyio.run(lambda: coro)


@pytest.fixture
def as_employee(monkeypatch):
    def _set(emp_id: int):
        monkeypatch.setattr(srv, "_transport", httpx.ASGITransport(app=app))
        monkeypatch.setattr(srv, "_token", None)
        monkeypatch.setenv("PEOPLELENS_EMPLOYEE_ID", str(emp_id))
    return _set


def answers(*responses):
    """Elicitation callback that replies with the given content dicts in order and records the prompts."""
    queue, prompts = list(responses), []

    async def cb(context, params: types.ElicitRequestParams):
        prompts.append(params.message)
        content = queue.pop(0)
        if content is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content=content)
    return cb, prompts


def payload(result) -> dict | list:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


async def call(tool: str, args: dict | None = None, cb=None):
    async with Client(srv.mcp, elicitation_callback=cb) as c:
        return await c.call_tool(tool, args or {})


def test_tools_listed():
    async def go():
        async with Client(srv.mcp) as c:
            return {t.name for t in (await c.list_tools()).tools}
    names = anyio.run(go)
    assert {"get_leave_balance", "apply_leave", "check_in", "search_policy", "pending_approvals"} <= names


def test_balance_tool(as_employee):
    as_employee(20)
    out = payload(anyio.run(call, "get_leave_balance", {"leave_type": "SL"}))
    assert out["balances"][0]["type"] == "SL" and out["balances"][0]["available"] == 9.0


def test_apply_leave_asks_for_details_then_confirms(as_employee):
    as_employee(30)  # Kavya
    cb, prompts = answers({"leave_type": "CL", "start": "2026-12-21", "end": "2026-12-22", "reason": "family"},
                          {"confirm": True})
    out = payload(anyio.run(call, "apply_leave", {}, cb))
    assert out["status"] == "submitted" and out["request_id"].startswith("LR-") and out["approver"] == "Vikram Singh"
    assert "Available balance" in prompts[0] and "Casual Leave" in prompts[1]
    with tx() as conn:
        row = conn.execute("SELECT status, approver_id FROM hr.leave_requests WHERE id=%s", (out["request_id"],)).fetchone()
        note = conn.execute("SELECT title FROM hr.notifications WHERE recipient_id=9 AND request_id=%s", (out["request_id"],)).fetchone()
    assert row["status"] == "pending" and row["approver_id"] == 9 and "Kavya Nair" in note["title"]
    # replaying the same application does not create a duplicate
    cb2, _ = answers({"confirm": True})
    again = payload(anyio.run(call, "apply_leave", {"leave_type": "CL", "start": "2026-12-21", "end": "2026-12-22"}, cb2))
    assert again["status"] in ("already_submitted", "not_eligible")


def test_apply_leave_declined_creates_nothing(as_employee):
    as_employee(20)
    cb, _ = answers({"leave_type": "SL", "start": "2026-12-28", "end": "", "reason": ""}, None)
    out = payload(anyio.run(call, "apply_leave", {}, cb))
    assert out["status"] == "cancelled"
    with tx() as conn:
        assert conn.execute("SELECT count(*) n FROM hr.leave_requests WHERE employee_id=20 AND start_date='2026-12-28'").fetchone()["n"] == 0


def test_apply_leave_not_eligible(as_employee):
    as_employee(21)  # Rohan, on probation
    out = payload(anyio.run(call, "apply_leave", {"leave_type": "EL", "start": "2026-12-14", "end": "2026-12-15"},
                            answers()[0]))
    assert out["status"] == "not_eligible" and any("probation" in r for r in out["reasons"])


def test_manager_decides_via_mcp_and_cannot_decide_others(as_employee):
    as_employee(9)  # Vikram
    pending = payload(anyio.run(call, "pending_approvals"))
    assert pending and all(p["request_id"].startswith("LR-") for p in pending)
    rid = pending[0]["request_id"]
    cb, _ = answers({"confirm": True})
    out = payload(anyio.run(call, "decide_leave_request", {"request_id": rid, "decision": "approve"}, cb))
    assert out["status"] == "approved"
    with tx() as conn:
        other = conn.execute("SELECT id FROM hr.leave_requests WHERE approver_id=8 AND status='pending' LIMIT 1").fetchone()["id"]
    cb, _ = answers({"confirm": True})
    res = anyio.run(call, "decide_leave_request", {"request_id": other, "decision": "approve"}, cb)
    assert res.is_error and "not assigned to you" in res.content[0].text


def test_apply_leave_handshake_era_client(as_employee):
    """Clients on the initialize-handshake protocol (e.g. current stdio clients) get mid-call elicitation requests."""
    as_employee(22)  # Sana
    cb, prompts = answers({"leave_type": "SL", "start": "2026-12-09", "end": "", "reason": ""}, {"confirm": True})

    async def go():
        async with Client(srv.mcp, mode="legacy", elicitation_callback=cb) as c:
            return await c.call_tool("apply_leave", {})
    out = payload(anyio.run(go))
    assert out["status"] == "submitted" and len(prompts) == 2


def test_client_without_elicitation_needs_explicit_confirm(as_employee):
    as_employee(20)
    out = payload(anyio.run(call, "apply_leave", {}))
    assert out["status"] == "needs_input" and "Casual Leave" in out["available_balance"]
    args = {"leave_type": "SL", "start": "2026-12-29"}
    out = payload(anyio.run(call, "apply_leave", args))
    assert out["status"] == "confirmation_required" and "Sick Leave" in out["summary"]
    with tx() as conn:
        assert conn.execute("SELECT count(*) n FROM hr.leave_requests WHERE employee_id=20 AND start_date='2026-12-29'").fetchone()["n"] == 0
    out = payload(anyio.run(call, "apply_leave", {**args, "confirm": True}))
    assert out["status"] == "submitted"
    again = payload(anyio.run(call, "apply_leave", {**args, "confirm": True}))   # retry: no duplicate
    assert again["status"] in ("already_submitted", "not_eligible")
    with tx() as conn:
        assert conn.execute("SELECT count(*) n FROM hr.leave_requests WHERE employee_id=20 AND start_date='2026-12-29'").fetchone()["n"] == 1


def test_check_in_and_policy_via_mcp(as_employee):
    as_employee(26)
    first = payload(anyio.run(call, "check_in", {"mode": "office"}))
    second = payload(anyio.run(call, "check_in", {"mode": "office"}))
    assert first["status"] == "present" and second["already_checked_in"]
    hits = payload(anyio.run(call, "search_policy", {"question": "Can I carry forward earned leave?"}))
    assert hits[0]["document"] == "leave_policy" and "15 days" in hits[0]["text"]
