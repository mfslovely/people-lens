"""Tool registry and executor.

Selection: the LLM sees only the tools the caller's role permits (layer 1). Every call is checked again here (layer 2)
and inside each tool against the database scope (layer 3). Arguments are validated by Pydantic before any code runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

import psycopg
from pydantic import BaseModel, ValidationError

from app.agent.models import Citation, Principal, ProposedAction, Role, ToolResult
from app.core.observability import Trace, ToolCallRecord, log
from app.security.authz import AuthzError


@dataclass
class ToolContext:
    principal: Principal
    today: date
    trace: Trace
    llm: Any = None                                  # for tools that need an LLM (text-to-SQL)
    citations: dict[str, Citation] = field(default_factory=dict)   # "[P1]" -> Citation
    evidence: list[Any] = field(default_factory=list)              # raw tool outputs, used by the grounding check
    proposed: ProposedAction | None = None
    entities: dict[str, Any] = field(default_factory=dict)         # memory written back to the session

    def add_citation(self, c: Citation) -> str:
        for ref, existing in self.citations.items():
            if existing.chunk_id == c.chunk_id:
                return ref
        ref = f"[P{len(self.citations) + 1}]"
        self.citations[ref] = c.model_copy(update={"ref": ref})
        return ref


@dataclass
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    roles: set[Role]
    fn: Callable[[ToolContext, Any], Any]
    kind: str = "read"     # read | propose

    def schema(self) -> dict:
        params = self.args.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": params}}


REGISTRY: dict[str, ToolSpec] = {}
ALL_ROLES = {Role.EMPLOYEE, Role.MANAGER, Role.HR_ADMIN}


def tool(name: str, description: str, args: type[BaseModel], roles: set[Role] | None = None, kind: str = "read"):
    def deco(fn):
        REGISTRY[name] = ToolSpec(name, description, args, roles or ALL_ROLES, fn, kind)
        return fn
    return deco


def tools_for(role: Role) -> list[ToolSpec]:
    return [t for t in REGISTRY.values() if role in t.roles]


def jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return obj


def execute(ctx: ToolContext, name: str, raw_args: dict | None) -> ToolResult:
    started = time.perf_counter()
    raw_args = raw_args or {}
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolResult(tool=name, ok=False, error=f"Unknown tool '{name}'.")

    def record(status: str, decision: str, error: str | None = None, retries: int = 0, chunks: list[int] | None = None):
        ctx.trace.tool_calls.append(ToolCallRecord(name, raw_args, decision, status, int((time.perf_counter() - started) * 1000),
                                                   retries, error, chunks or []))

    if ctx.principal.role not in spec.roles:
        msg = f"The tool '{name}' is not available to the {ctx.principal.role.value} role."
        record("denied", "deny:role")
        ctx.trace.audit("authz_denied", {"tool": name, "reason": "role"})
        return ToolResult(tool=name, ok=False, error=msg)
    try:
        args = spec.args.model_validate(raw_args)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        record("error", "allow", f"invalid_args: {errs}")
        return ToolResult(tool=name, ok=False, error=f"Invalid arguments: {errs}")

    retries = 0
    while True:
        try:
            data = spec.fn(ctx, args)
            data = jsonable(data)
            chunks = [c.chunk_id for c in ctx.citations.values()] if name == "search_policy" else []
            record("ok", "allow", retries=retries, chunks=chunks)
            ctx.evidence.append({"tool": name, "data": data})
            return ToolResult(tool=name, ok=True, data=data)
        except AuthzError as e:
            record("denied", f"deny:{e}"[:200])
            ctx.trace.audit("authz_denied", {"tool": name, "reason": str(e)})
            return ToolResult(tool=name, ok=False, error=f"PERMISSION_DENIED: {e}")
        except (ValueError, LookupError) as e:
            record("error", "allow", str(e))
            return ToolResult(tool=name, ok=False, error=str(e))
        except psycopg.OperationalError as e:
            if retries < 1:
                retries += 1
                time.sleep(0.3)
                continue
            record("error", "allow", "database_unavailable", retries)
            log.error("tool_db_error", extra={"data": {"tool": name, "error": str(e)}})
            return ToolResult(tool=name, ok=False, error="DATABASE_UNAVAILABLE: the data service is temporarily unavailable.")
        except Exception as e:  # noqa: BLE001
            record("error", "allow", type(e).__name__, retries)
            log.exception("tool_failed", extra={"data": {"tool": name}})
            return ToolResult(tool=name, ok=False, error=f"TOOL_ERROR: {name} failed unexpectedly ({type(e).__name__}).")
