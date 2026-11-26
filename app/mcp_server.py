"""People Lens MCP server: leave, attendance and policy tools for any MCP-compatible client.

The server is a thin, authenticated client of the People Lens API. It holds no database credentials, and every call
is authorized by the backend under the employee's own identity, exactly like the web app.

Run (stdio):  PEOPLELENS_EMPLOYEE_ID=20 python -m app.mcp_server
Run (HTTP):   PEOPLELENS_EMPLOYEE_ID=20 python -m app.mcp_server --http --port 8765
Environment:  PEOPLELENS_API_URL (default http://127.0.0.1:8000)
              PEOPLELENS_TOKEN (a JWT from /auth/login) or PEOPLELENS_EMPLOYEE_ID (demo sign-in as a seeded employee)

Applying for leave is interactive. `apply_leave` shows the available balance and asks the employee for the leave type
and dates through MCP elicitation, checks eligibility, then asks for explicit confirmation before creating the request.
The request then appears in the approver's dashboard and the HR view. For clients without elicitation support, the
tool returns what is missing (with balances), and submission requires `confirm=true` after the user has agreed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, Field

from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError

API_URL = os.getenv("PEOPLELENS_API_URL", "http://127.0.0.1:8000")
LEAVE_TYPES = {"CL": "Casual Leave", "SL": "Sick Leave", "EL": "Earned Leave", "LOP": "Loss of Pay"}

mcp = MCPServer(
    "People Lens",
    instructions=("HR assistant tools for the signed-in employee: leave balances, leave applications (with confirmation), "
                  "leave status, attendance check-in/out, notifications, policy search, and approvals for managers. "
                  "Always cite the policy section returned by search_policy. Never invent balances or dates."),
)

# ------------------------------------------------------------------ API client
_transport: httpx.AsyncBaseTransport | None = None   # tests inject an ASGI transport
_token: str | None = os.getenv("PEOPLELENS_TOKEN")


async def _login(c: httpx.AsyncClient) -> str:
    global _token
    if not _token:
        emp = os.getenv("PEOPLELENS_EMPLOYEE_ID")
        if not emp:
            raise ToolError("Not signed in: set PEOPLELENS_TOKEN or PEOPLELENS_EMPLOYEE_ID for the MCP server.")
        r = await c.post("/auth/login", json={"employee_id": int(emp)})
        if r.status_code != 200:
            raise ToolError(f"Sign-in failed: {r.text}")
        _token = r.json()["token"]
    return _token


async def _api(method: str, path: str, **kw) -> Any:
    async with httpx.AsyncClient(base_url=API_URL, transport=_transport, timeout=60) as c:
        try:
            token = await _login(c)
            headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {token}"}
            r = await c.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as e:
            raise ToolError(f"People Lens API is unreachable at {API_URL}.") from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise ToolError(detail if isinstance(detail, str) else f"Request failed ({r.status_code}).")
    return r.json()


def _fmt_balances(balances: list[dict]) -> str:
    return ", ".join(f"{b['name']} ({b['leave_type']}): {float(b['available']):g} days" for b in balances)


def _supports_elicitation(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    return bool(caps and getattr(caps, "elicitation", None))


async def _resolve(expression: str) -> dict:
    return await _api("POST", "/dates/resolve", json={"expression": expression})


# ------------------------------------------------------------------ read tools
@mcp.tool()
async def whoami() -> dict:
    """Who is signed in: name, role, department, manager and today's date."""
    me = await _api("GET", "/me")
    return {k: me[k] for k in ("name", "role", "designation", "department", "manager", "today")}


@mcp.tool()
async def get_leave_balance(leave_type: Literal["CL", "SL", "EL"] | None = None) -> dict:
    """Available leave balance for the signed-in employee (CL = casual, SL = sick, EL = earned)."""
    data = await _api("GET", "/me/balances")
    rows = [b for b in data["balances"] if leave_type in (None, b["leave_type"])]
    return {"year": data["year"], "balances": [{"type": b["leave_type"], "name": b["name"], "available": b["available"],
                                                "used": b["used"], "pending": b["pending"]} for b in rows]}


@mcp.tool()
async def my_leave_requests(status: Literal["pending", "approved", "rejected", "cancelled"] | None = None) -> list[dict]:
    """The signed-in employee's leave requests and their status."""
    rows = await _api("GET", "/me/leave-requests")
    return [{"id": r["id"], "type": r["leave_type"], "start": r["start_date"], "end": r["end_date"], "days": r["days"],
             "status": r["status"], "approver": r["approver"]} for r in rows if status in (None, r["status"])][:30]


@mcp.tool()
async def check_leave_eligibility(leave_type: Literal["CL", "SL", "EL", "LOP"], start: str, end: str | None = None) -> dict:
    """Check whether a leave can be taken. Dates accept YYYY-MM-DD or phrases like 'next Monday'."""
    s = await _resolve(start)
    e = await _resolve(end) if end else s
    return await _api("POST", "/leave/eligibility", json={"leave_type": leave_type, "start_date": s["start_date"],
                                                          "end_date": e["end_date"]})


@mcp.tool()
async def search_policy(question: str) -> list[dict]:
    """Search HR policy documents (leave, holidays, approvals, WFH, attendance, handbook). Cite the returned sections."""
    hits = await _api("POST", "/policy/search", json={"query": question})
    if not hits:
        return [{"note": "No policy covers this question. Suggest contacting HR."}]
    return [{"document": h["document"], "version": h["version"], "section": h["section"], "text": h["text"]} for h in hits[:4]]


@mcp.tool()
async def notifications() -> dict:
    """Latest notifications: decisions on your leave and, for approvers, new requests to review."""
    data = await _api("GET", "/notifications", params={"limit": 10})
    return {"unread": data["unread"], "items": [{"title": n["title"], "detail": n["body"], "unread": not n["is_read"],
                                                  "at": n["created_at"]} for n in data["items"]]}


# ------------------------------------------------------------------ attendance
@mcp.tool()
async def check_in(mode: Literal["office", "wfh"] = "office") -> dict:
    """Check in for today, at the office or working from home."""
    return await _api("POST", "/attendance/check-in", json={"mode": mode})


@mcp.tool()
async def check_out() -> dict:
    """Check out for today."""
    return await _api("POST", "/attendance/check-out")


# ------------------------------------------------------------------ apply for leave (interactive)
# Interaction uses MCP resolvers: a resolver returns `Elicit(...)` to ask the employee, and the SDK carries the question
# over whichever protocol the client negotiated (a mid-call elicitation request, or an input-required round trip).
# Tool arguments the model already supplied are used as-is; only missing details are asked for.

class LeaveDetails(BaseModel):
    leave_type: Literal["CL", "SL", "EL", "LOP"] = Field(description="CL casual, SL sick, EL earned, LOP loss of pay")
    start: str = Field(description="First day: YYYY-MM-DD or e.g. 'next Monday'")
    end: str = Field("", description="Last day (leave empty for a single day)")
    reason: str = Field("", description="Optional reason")


class Confirm(BaseModel):
    confirm: bool = Field(description="Submit this?")


async def _ask_details(ctx: Context, leave_type: str | None, start: str | None, end: str | None,
                       reason: str | None) -> LeaveDetails | Elicit[LeaveDetails] | None:
    if leave_type and start:
        return LeaveDetails(leave_type=leave_type, start=start, end=end or "", reason=reason or "")
    if not _supports_elicitation(ctx):
        return None                                   # the tool body reports what is missing
    balances = (await _api("GET", "/me/balances"))["balances"]
    return Elicit(f"Available balance: {_fmt_balances(balances)}.\nWhich leave would you like to apply for, and when?",
                  LeaveDetails)


async def _plan(details: Annotated[ElicitationResult[LeaveDetails], Resolve(_ask_details)], half_day: bool) -> dict:
    if not isinstance(details, AcceptedElicitation):
        return {"status": "cancelled", "message": "No leave request was created."}
    d = details.data
    if d is None:
        balances = (await _api("GET", "/me/balances"))["balances"]
        return {"status": "needs_input", "available_balance": _fmt_balances(balances),
                "ask_user": "Which leave type (CL, SL, EL or LOP) and which dates would you like?"}
    s = await _resolve(d.start)
    e = await _resolve(d.end) if d.end else s
    payload = {"leave_type": d.leave_type, "start_date": s["start_date"], "end_date": e["end_date"], "half_day": half_day,
               "reason": d.reason or None}
    el = await _api("POST", "/leave/eligibility", json=payload)
    if not el["eligible"]:
        return {"status": "not_eligible", "reasons": el["violations"], "available": el["balance_available"]}
    after = el["balance_after"] if el["balance_after"] is not None else "n/a"
    summary = (f"{LEAVE_TYPES[d.leave_type]} from {payload['start_date']} to {payload['end_date']} "
               f"({el['working_days_requested']:g} working day(s)). Balance after: {after}. Approver: {el['approver_name']}.")
    return {"status": "ready", "payload": payload, "summary": summary, "approver": el["approver_name"],
            "warnings": el["warnings"]}


async def _ask_confirm(ctx: Context, plan: Annotated[dict, Resolve(_plan)], confirm: bool) -> Confirm | Elicit[Confirm]:
    if plan["status"] != "ready":
        return Confirm(confirm=False)
    if not _supports_elicitation(ctx):
        return Confirm(confirm=confirm)
    return Elicit(f"Please confirm: {plan['summary']}", Confirm)


@mcp.tool()
async def apply_leave(ctx: Context, plan: Annotated[dict, Resolve(_plan)],
                      approval: Annotated[ElicitationResult[Confirm], Resolve(_ask_confirm)],
                      leave_type: Literal["CL", "SL", "EL", "LOP"] | None = None, start: str | None = None,
                      end: str | None = None, reason: str | None = None, half_day: bool = False,
                      confirm: bool = False) -> dict:
    """Apply for leave for the signed-in employee.

    Shows the available balance and asks for any missing leave type or dates, checks eligibility against policy,
    and asks the employee to confirm before submitting. The request then goes to the approver's dashboard and HR.
    Dates accept YYYY-MM-DD or phrases like 'next Monday'. Never set confirm=true unless the employee explicitly
    agreed to the exact summary."""
    if plan["status"] != "ready":
        return plan
    if not isinstance(approval, AcceptedElicitation):
        return {"status": "cancelled", "message": "No leave request was created."}
    if not approval.data.confirm:
        if _supports_elicitation(ctx):
            return {"status": "cancelled", "message": "No leave request was created."}
        return {"status": "confirmation_required", "summary": plan["summary"], "warnings": plan["warnings"],
                "next": "Show this summary to the employee. If they explicitly agree, call apply_leave again with "
                        "the same details and confirm=true. If they decline, do nothing."}
    p = plan["payload"]
    key = hashlib.sha256(f"{p['leave_type']}|{p['start_date']}|{p['end_date']}|{p['half_day']}".encode()).hexdigest()[:32]
    out = await _api("POST", "/leave-requests", json=p, headers={"Idempotency-Key": key})
    return {"status": "already_submitted" if out["duplicate"] else "submitted", "request_id": out["request_id"],
            "request_status": out["status"], "approver": out.get("approver") or plan["approver"], "summary": plan["summary"]}


# ------------------------------------------------------------------ approvals (managers)
@mcp.tool()
async def pending_approvals() -> list[dict]:
    """Leave requests waiting for the signed-in user's decision (approvers only)."""
    rows = await _api("GET", "/approvals")
    return [{"request_id": r["id"], "employee": r["employee"], "type": r["leave_type"], "start": r["start_date"],
             "end": r["end_date"], "days": r["days"], "reason": r["reason"], "teammates_away": r["teammates_away"]} for r in rows]


async def _ask_decision(ctx: Context, request_id: str, decision: str, confirm: bool) -> Confirm | Elicit[Confirm]:
    if not _supports_elicitation(ctx):
        return Confirm(confirm=confirm)
    return Elicit(f"{decision.capitalize()} leave request {request_id}?", Confirm)


@mcp.tool()
async def decide_leave_request(ctx: Context, approval: Annotated[ElicitationResult[Confirm], Resolve(_ask_decision)], request_id: str,
                               decision: Literal["approve", "reject"], comment: str | None = None,
                               confirm: bool = False) -> dict:
    """Approve or reject a leave request assigned to you, after confirmation.
    You can't decide your own requests or ones assigned to someone else."""
    if not isinstance(approval, AcceptedElicitation):
        return {"status": "cancelled"}
    if not approval.data.confirm:
        if _supports_elicitation(ctx):
            return {"status": "cancelled"}
        return {"status": "confirmation_required", "next": "Ask the user to confirm, then call again with confirm=true."}
    return await _api("POST", f"/leave-requests/{request_id}/decision", json={"decision": decision, "comment": comment})


def main() -> None:
    ap = argparse.ArgumentParser(description="People Lens MCP server")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.http:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
