"""Tracing: JSON logs plus rows in ops.traces, ops.tool_calls and ops.audit_log.

Personal data is redacted before anything is logged or persisted.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

_trace: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar("trace", default=None)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_SECRET = re.compile(r"(xai-[A-Za-z0-9]{6})[A-Za-z0-9]+|(sk-[A-Za-z0-9]{4})[A-Za-z0-9]+|(eyJ[\w-]{6})[\w.-]+")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        value = _EMAIL.sub("<email>", value)
        return _SECRET.sub(lambda m: (m.group(1) or m.group(2) or m.group(3)) + "…", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        t = _trace.get()
        if t:
            payload["trace_id"] = t.trace_id
        extra = getattr(record, "data", None)
        if extra:
            payload.update(redact(extra))
        return json.dumps(payload, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "openai", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger("peoplelens")


@dataclass
class ToolCallRecord:
    tool_name: str
    args: dict
    authz_decision: str
    status: str
    latency_ms: int
    retries: int = 0
    error: str | None = None
    retrieved_chunk_ids: list[int] = field(default_factory=list)


@dataclass
class Trace:
    session_id: str | None
    user_id: int | None
    role: str | None
    trace_id: str = field(default_factory=lambda: "tr_" + uuid.uuid4().hex[:16])
    started: float = field(default_factory=time.perf_counter)
    intents: list[str] = field(default_factory=list)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    fallback_used: bool = False
    status: str = "ok"
    error_code: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    audits: list[tuple[str, dict]] = field(default_factory=list)

    @property
    def latency_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def add_usage(self, model: str, usage: Any) -> None:
        self.llm_calls += 1
        self.model = model
        if usage is not None:
            self.input_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.output_tokens += getattr(usage, "completion_tokens", 0) or 0

    def audit(self, event: str, detail: dict) -> None:
        self.audits.append((event, detail))
        log.warning("audit", extra={"data": {"event": event, **detail}})

    def cost_usd(self) -> float:
        from app.config import get_settings

        s = get_settings()
        return round(self.input_tokens / 1e6 * s.price_in_per_m + self.output_tokens / 1e6 * s.price_out_per_m, 6)

    def summary(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "intents": self.intents,
            "model": self.model,
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd(),
            "latency_ms": self.latency_ms,
            "status": self.status,
            "error_code": self.error_code,
            "fallback_used": self.fallback_used,
            "tools": [
                {"tool": c.tool_name, "status": c.status, "authz": c.authz_decision, "latency_ms": c.latency_ms,
                 "chunks": c.retrieved_chunk_ids, "error": c.error}
                for c in self.tool_calls
            ],
        }


def start_trace(session_id: str | None, user_id: int | None, role: str | None) -> Trace:
    t = Trace(session_id=session_id, user_id=user_id, role=role)
    _trace.set(t)
    return t


def current_trace() -> Trace | None:
    return _trace.get()


def persist_trace(t: Trace) -> None:
    """Best effort: a failure to write telemetry must never fail the user's request."""
    from app.db import tx

    try:
        with tx() as conn:
            conn.execute(
                """INSERT INTO ops.traces (trace_id, session_id, user_id, role, intents, model, input_tokens,
                       output_tokens, cost_usd, latency_ms, status, error_code, fallback_used)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (trace_id) DO NOTHING""",
                (t.trace_id, t.session_id, t.user_id, t.role, t.intents, t.model, t.input_tokens, t.output_tokens,
                 t.cost_usd(), t.latency_ms, t.status, t.error_code, t.fallback_used),
            )
            for c in t.tool_calls:
                conn.execute(
                    """INSERT INTO ops.tool_calls (trace_id, tool_name, args_redacted, authz_decision, status,
                           latency_ms, retries, error, retrieved_chunk_ids) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (t.trace_id, c.tool_name, Jsonb(redact(c.args)), c.authz_decision, c.status, c.latency_ms,
                     c.retries, c.error, c.retrieved_chunk_ids or None),
                )
            for event, detail in t.audits:
                conn.execute(
                    "INSERT INTO ops.audit_log (trace_id, actor_id, event, detail) VALUES (%s,%s,%s,%s)",
                    (t.trace_id, t.user_id, event, Jsonb(redact(detail))),
                )
    except Exception as exc:  # noqa: BLE001
        log.error("trace_persist_failed", extra={"data": {"error": str(exc)}})
    log.info("request_complete", extra={"data": t.summary()})
