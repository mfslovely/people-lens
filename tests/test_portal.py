"""Portal API: dashboard, approvals with buttons, HR view, notifications, attendance."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.db import tx
from app.services import attendance
from tests.conftest import make_principal

client = TestClient(app)


def h(emp: int) -> dict:
    return {"Authorization": f"Bearer {client.post('/auth/login', json={'employee_id': emp}).json()['token']}"}


def test_me_and_balances():
    me = client.get("/me", headers=h(8)).json()
    assert me["name"] == "Priya Sharma" and me["role"] == "manager" and me["direct_reports"] >= 7
    b = client.get("/me/balances", headers=h(20)).json()["balances"]
    assert [x["leave_type"] for x in b] == ["CL", "SL", "EL"]


def test_apply_notifies_approver_and_is_idempotent():
    emp = h(29)
    body = {"leave_type": "CL", "start_date": "2026-12-22", "end_date": "2026-12-22", "reason": "errand"}
    r1 = client.post("/leave-requests", json=body, headers={**emp, "Idempotency-Key": "test-key-0001"})
    r2 = client.post("/leave-requests", json=body, headers={**emp, "Idempotency-Key": "test-key-0001"})
    assert r1.status_code == 201 and r2.json()["duplicate"] and r1.json()["request_id"] == r2.json()["request_id"]
    rid = r1.json()["request_id"]
    mgr = h(8)
    assert any(a["id"] == rid for a in client.get("/approvals", headers=mgr).json())
    notes = client.get("/notifications", headers=mgr).json()
    assert any(n["request_id"] == rid and n["kind"] == "approval_needed" and not n["is_read"] for n in notes["items"])
    # HR sees it in the org-wide list
    assert any(x["id"] == rid for x in client.get("/hr/leave-requests", params={"q": rid}, headers=h(13)).json())
    # the manager approves with the button endpoint, and the employee is notified
    d = client.post(f"/leave-requests/{rid}/decision", json={"decision": "approve"}, headers=mgr)
    assert d.json()["status"] == "approved"
    mine = client.get("/notifications", headers=emp).json()["items"]
    assert any(n["request_id"] == rid and n["kind"] == "leave_approved" for n in mine)
    assert client.post(f"/leave-requests/{rid}/decision", json={"decision": "reject"}, headers=mgr).status_code == 422


def test_ineligible_apply_is_rejected_with_reason():
    r = client.post("/leave-requests", json={"leave_type": "CL", "start_date": "2026-12-07", "end_date": "2026-12-11"},
                    headers={**h(20), "Idempotency-Key": "test-key-0002"})
    assert r.status_code == 422 and "consecutive" in r.json()["detail"]


def test_decision_authorization():
    with tx() as conn:
        own = conn.execute("SELECT id FROM hr.leave_requests WHERE employee_id=8 AND status='pending' LIMIT 1").fetchone()["id"]
        others = conn.execute("SELECT id FROM hr.leave_requests WHERE approver_id=9 AND status='pending' LIMIT 1").fetchone()["id"]
    mgr = h(8)
    assert client.post(f"/leave-requests/{own}/decision", json={"decision": "approve"}, headers=mgr).status_code == 403
    assert client.post(f"/leave-requests/{others}/decision", json={"decision": "approve"}, headers=mgr).status_code == 403
    assert client.post(f"/leave-requests/{others}/decision", json={"decision": "approve"}, headers=h(20)).status_code == 403


def test_hr_endpoints_are_hr_only_and_include_managers():
    assert client.get("/hr/leave-requests", headers=h(8)).status_code == 403
    assert client.get("/hr/summary", headers=h(20)).status_code == 403
    rows = client.get("/hr/leave-requests", params={"role": "manager"}, headers=h(13)).json()
    assert rows and {r["employee"] for r in rows} & {"Priya Sharma", "Anil Kumar"}
    assert client.get("/hr/summary", headers=h(13)).json()["headcount"] > 200


def test_employee_leave_view_is_scoped():
    assert client.get("/employees/30/leave", headers=h(13)).status_code == 200       # HR: anyone
    assert client.get("/employees/8/leave", headers=h(13)).status_code == 200        # HR: managers too
    assert client.get("/employees/20/leave", headers=h(8)).status_code == 200        # manager: own report
    assert client.get("/employees/30/leave", headers=h(8)).status_code == 403        # manager: other team
    assert client.get("/employees/30/leave", headers=h(20)).status_code == 403       # employee: someone else


def test_check_in_rules():
    emp = h(28)
    first = client.post("/attendance/check-in", json={"mode": "office"}, headers=emp).json()
    again = client.post("/attendance/check-in", json={"mode": "office"}, headers=emp).json()
    assert first["status"] == "present" and again["already_checked_in"]
    out = client.post("/attendance/check-out", headers=emp).json()
    assert out["check_out"] and client.get("/attendance/today", headers=emp).json()["check_out"]
    # probation employees cannot mark WFH
    r = client.post("/attendance/check-in", json={"mode": "wfh"}, headers=h(21))
    assert r.status_code == 422 and "probation" in r.json()["detail"]
    # checking out without checking in fails
    assert client.post("/attendance/check-out", headers=h(25)).status_code == 422


def test_late_flag():
    p = make_principal(24)
    res = attendance.check_in(p, "office", now=datetime(2026, 11, 25, 10, 40))
    assert res["late"] is True


def test_notifications_mark_read():
    emp = h(9)
    before = client.get("/notifications", headers=emp).json()["unread"]
    assert before > 0
    client.post("/notifications/read", json={}, headers=emp)
    assert client.get("/notifications", headers=emp).json()["unread"] == 0


@pytest.mark.parametrize("path", ["/me", "/approvals", "/notifications", "/attendance/today", "/me/leave-requests"])
def test_auth_required(path):
    assert client.get(path).status_code == 401
