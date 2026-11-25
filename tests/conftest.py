from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import pytest

from app.agent.llm import LLMReply, LLMUnavailable, ToolCall
from app.agent.models import Principal, Role
from app.db import tx

PERSONA = {"arjun": 20, "rohan": 21, "priya": 8, "vikram": 9, "kavya": 30, "neha": 13, "sana": 22}


@pytest.fixture(scope="session", autouse=True)
def seeded():
    """Fresh deterministic database for the test session."""
    from app.rag import ingest
    from scripts import seed_data

    seed_data.main()
    ingest.main([])
    yield


def make_principal(name_or_id: str | int) -> Principal:
    emp = PERSONA[name_or_id] if isinstance(name_or_id, str) else name_or_id
    with tx() as conn:
        u = conn.execute("SELECT id, role FROM hr.app_users WHERE employee_id=%s", (emp,)).fetchone()
        sid = conn.execute("INSERT INTO agent.sessions (user_id, role_at_start) VALUES (%s,%s) RETURNING id",
                           (u["id"], u["role"])).fetchone()["id"]
    return Principal(user_id=u["id"], employee_id=emp, role=Role(u["role"]), session_id=sid)


@pytest.fixture
def principal() -> Callable[[str | int], Principal]:
    return make_principal


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{uuid.uuid4().hex[:6]}", name=name, arguments=args, raw_arguments=json.dumps(args))


class FakeLLM:
    """Scripted stand-in for Grok. Router calls get `router` JSON; agent calls pop replies from `script`.

    A script item may be an LLMReply or a callable(messages) -> LLMReply, so a test can react to tool results.
    """

    def __init__(self, script: list | None = None, router: dict | None = None, down: bool = False):
        self.script = list(script or [])
        self.router = router or {"intents": ["personal_data"], "confidence": 0.9, "needs_clarification": None}
        self.down = down
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, tier="primary", json_mode=False):
        self.calls.append({"tier": tier, "json_mode": json_mode, "n_tools": len(tools or []),
                           "tool_names": [t["function"]["name"] for t in tools or []], "messages": messages})
        if self.down:
            raise LLMUnavailable("simulated outage")
        if json_mode:
            return LLMReply(content=json.dumps(self.router), model="fake-fast")
        if not self.script:
            return LLMReply(content="(no more scripted replies)", model="fake")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


def reply(text: str | None = None, *calls: ToolCall) -> LLMReply:
    return LLMReply(content=text, tool_calls=list(calls), model="fake-primary")


def last_tool_result(messages: list[dict]) -> dict:
    return json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])
