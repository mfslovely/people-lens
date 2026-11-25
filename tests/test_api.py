"""HTTP layer: authentication, session isolation, confirmation endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.graph import Orchestrator
from app.api import main
from tests.conftest import FakeLLM, call, reply


@pytest.fixture
def client(monkeypatch):
    fake = FakeLLM(script=[reply(None, call("propose_leave", leave_type="SL", start_date="2026-11-23", end_date="2026-11-23")),
                           reply("Prepared a sick leave proposal; please confirm.")])
    monkeypatch.setattr(main, "orchestrator", lambda: Orchestrator(fake))
    return TestClient(main.app)


def login(client, emp):
    t = client.post("/auth/login", json={"employee_id": emp}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def test_health(client):
    assert client.get("/health").json()["db"] == "ok"


def test_requires_auth(client):
    assert client.post("/sessions").status_code == 401
    assert client.post("/sessions", headers={"Authorization": "Bearer forged.token.value"}).status_code == 401


def test_session_isolation_and_confirm_flow(client):
    a, b = login(client, 21), login(client, 9)   # Rohan (employee), Vikram (manager)
    sid = client.post("/sessions", headers=a).json()["session_id"]
    r = client.post("/chat", headers=a, json={"session_id": sid, "message": "I was sick on Monday, log it"}).json()
    aid = r["pending_action"]["action_id"]
    # another user can neither read the session nor confirm the action through it
    assert client.get(f"/sessions/{sid}/messages", headers=b).status_code == 404
    assert client.post("/chat", headers=b, json={"session_id": sid, "message": "hi"}).status_code == 404
    assert client.post(f"/actions/{aid}/confirm", headers=b, json={"session_id": sid}).status_code == 404
    bsid = client.post("/sessions", headers=b).json()["session_id"]
    assert client.post(f"/actions/{aid}/confirm", headers=b, json={"session_id": bsid}).json()["outcome"]["status"] == "failed"
    # the owner confirms twice: one request
    o1 = client.post(f"/actions/{aid}/confirm", headers=a, json={"session_id": sid}).json()["outcome"]
    o2 = client.post(f"/actions/{aid}/confirm", headers=a, json={"session_id": sid}).json()["outcome"]
    assert o1["status"] == "executed" and o2["status"] == "already_executed" and o1["request_id"] == o2["request_id"]
    msgs = client.get(f"/sessions/{sid}/messages", headers=a).json()
    assert any("LR-" in m["content"] for m in msgs)
