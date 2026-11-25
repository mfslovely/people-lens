"""LangGraph orchestration for one conversational turn.

    guard ──blocked──────────────────────────────────────────────► finalize
      │
    pending_action ──"yes"/"no" with an open proposal (deterministic)──► finalize
      │
    route (fast model; heuristic fallback) ──unsupported / clarify──► finalize
      │
    agent (primary model + role-filtered tools, ReAct loop) ◄──┐
      │                                                        │ one regeneration
    verify (grounding + no-false-success check) ──fail once────┘
      │
    finalize ─► AgentResponse
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.agent import memory, prompts
from app.agent.llm import LLM, ChatLLM, LLMUnavailable
from app.agent.models import AgentResponse, Citation, Intent, Principal, RouteDecision
from app.config import get_settings
from app.core.dates import week_start
from app.core.observability import log, persist_trace, start_trace
from app.db import tx
from app.security.guards import CANCEL_RE, CONFIRM_RE, check_grounding, check_input
from app.tools import ToolContext, execute, tools_for
from app.tools.actions import cancel_action, confirm_action, open_proposal

_SUCCESS_CLAIM = re.compile(
    r"\b(has|have) been (successfully )?(submitted|approved|rejected|applied)\b|\bsuccessfully (submitted|approved|rejected|applied)\b"
    r"|\bI(?:'ve| have) (submitted|approved|rejected|applied)\b", re.I)
_POLICY_HINT = re.compile(r"\b(polic|entitle|carry|forward|holiday|wfh|work from home|notice|probation|rule|allowed|"
                          r"approv\w* leave|escalat|unavailable|attendance|late|handbook|sick|casual|earned|maternity)\w*", re.I)
MAX_TOOL_CONTENT = 7000


class TurnState(TypedDict, total=False):
    message: str
    ctx: ToolContext
    history: list[dict]
    memory: dict
    route: RouteDecision
    llm_messages: list[dict]
    draft: str
    retries: int
    status: str            # ok | blocked | action | clarify | unsupported | degraded | error
    issues: list[str]
    final: AgentResponse


class Orchestrator:
    def __init__(self, llm: LLM | None = None) -> None:
        self.llm = llm or ChatLLM()
        self.s = get_settings()
        self.graph = self._build()

    # ------------------------------------------------------------------ public
    def run(self, principal: Principal, message: str) -> AgentResponse:
        trace = start_trace(str(principal.session_id), principal.user_id, principal.role.value)
        ctx = ToolContext(principal=principal, today=self.s.today, trace=trace, llm=self.llm)
        try:
            state = self.graph.invoke({
                "message": message.strip(), "ctx": ctx, "retries": 0, "issues": [],
                "history": memory.history(str(principal.session_id), self.s.history_turns),
                "memory": memory.entities(str(principal.session_id)),
            })
            resp: AgentResponse = state["final"]
            trace.status = state.get("status", "ok")
        except Exception as e:  # noqa: BLE001  (last-resort safety net)
            log.exception("orchestrator_failed")
            trace.status, trace.error_code = "error", type(e).__name__
            resp = AgentResponse(answer="Something went wrong while processing your request. Nothing was changed. "
                                        f"Please try again (reference {trace.trace_id}).",
                                 confidence="abstained", trace_id=trace.trace_id)
        try:
            memory.save_turn(principal, message, resp.answer, [c.model_dump() for c in resp.citations], trace.trace_id, ctx.entities)
        except Exception:  # noqa: BLE001
            log.exception("memory_save_failed")
        persist_trace(trace)
        return resp.model_copy(update={"trace": trace.summary()})

    # ------------------------------------------------------------------ graph
    def _build(self):
        g = StateGraph(TurnState)
        g.add_node("guard", self._guard)
        g.add_node("pending_action", self._pending_action)
        g.add_node("router", self._route)
        g.add_node("agent", self._agent)
        g.add_node("verify", self._verify)
        g.add_node("finalize", self._finalize)
        g.set_entry_point("guard")
        g.add_conditional_edges("guard", lambda s: "finalize" if s.get("status") == "blocked" else "pending_action")
        g.add_conditional_edges("pending_action", lambda s: "finalize" if s.get("status") == "action" else "router")
        g.add_conditional_edges("router", lambda s: "finalize" if s.get("status") in ("clarify", "unsupported") else "agent")
        g.add_conditional_edges("agent", lambda s: "verify" if s.get("status") == "ok" else "finalize")
        g.add_conditional_edges("verify", lambda s: "agent" if s.get("status") == "regenerate" else "finalize")
        g.add_edge("finalize", END)
        return g.compile()

    # ------------------------------------------------------------------ nodes
    def _guard(self, s: TurnState) -> dict:
        v = check_input(s["message"])
        if v.allowed:
            return {}
        s["ctx"].trace.audit("input_blocked", {"category": v.category})
        return {"status": "blocked", "draft": v.reason, "issues": [f"blocked:{v.category}"]}

    def _pending_action(self, s: TurnState) -> dict:
        msg, ctx = s["message"], s["ctx"]
        if not (CONFIRM_RE.match(msg) or CANCEL_RE.match(msg)):
            return {}
        prop = open_proposal(ctx.principal)
        if prop is None:
            return {}
        if CANCEL_RE.match(msg):
            out = cancel_action(ctx.principal, str(prop["id"]))
            text = "Cancelled. Nothing was submitted." if out.status == "cancelled" else f"Could not cancel: {out.error}"
        else:
            out = confirm_action(ctx.principal, str(prop["id"]), ctx.trace.trace_id)
            text = action_outcome_text(prop, out)
        ctx.trace.intents = ["action_confirmation"]
        ctx.entities["last_action"] = {"action_id": str(prop["id"]), "status": out.status, "request_id": out.request_id}
        return {"status": "action", "draft": text}

    def _route(self, s: TurnState) -> dict:
        ctx = s["ctx"]
        hist = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in s["history"][-4:]) or "(none)"
        try:
            reply = self.llm.chat(
                [{"role": "system", "content": prompts.ROUTER.format(role=ctx.principal.role.value, today=ctx.today, history=hist)},
                 {"role": "user", "content": s["message"]}], tier="fast", json_mode=True)
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", (reply.content or "").strip(), flags=re.M))
            intents = [Intent(i) for i in data.get("intents", []) if i in Intent._value2member_map_] or [Intent.PERSONAL_DATA]
            route = RouteDecision(intents=intents, confidence=float(data.get("confidence", 0.5)),
                                  entities=data.get("entities") or {}, needs_clarification=data.get("needs_clarification"))
        except LLMUnavailable:
            route = heuristic_route(s["message"])
        except (ValueError, json.JSONDecodeError, TypeError):
            route = heuristic_route(s["message"])
        ctx.trace.intents = [i.value for i in route.intents]
        if route.intents == [Intent.UNSUPPORTED]:
            return {"route": route, "status": "unsupported", "draft": prompts.UNSUPPORTED_REPLY}
        if Intent.AMBIGUOUS in route.intents and route.needs_clarification and len(route.intents) == 1:
            return {"route": route, "status": "clarify", "draft": route.needs_clarification}
        return {"route": route}

    def _system_prompt(self, ctx: ToolContext, mem: dict) -> str:
        with tx() as conn:
            me = conn.execute("""SELECT e.full_name, e.emp_code, d.name AS dept FROM hr.employees e
                                 JOIN hr.departments d ON d.id=e.department_id WHERE e.id=%s""",
                              (ctx.principal.employee_id,)).fetchone()
        nws = week_start(ctx.today) + timedelta(days=7)
        return prompts.SYSTEM.format(
            name=me["full_name"], employee_id=ctx.principal.employee_id, emp_code=me["emp_code"], department=me["dept"],
            role=ctx.principal.role.value, role_scope=prompts.ROLE_SCOPE[ctx.principal.role.value],
            today=ctx.today.isoformat(), weekday=ctx.today.strftime("%A"), next_week_start=nws.isoformat(),
            next_week_end=(nws + timedelta(days=4)).isoformat(),
            memory=json.dumps(mem, default=str) if mem else "(empty)")

    def _agent(self, s: TurnState) -> dict:
        ctx = s["ctx"]
        msgs = s.get("llm_messages") or [
            {"role": "system", "content": self._system_prompt(ctx, s["memory"])}, *s["history"],
            {"role": "user", "content": s["message"]}]
        schemas = [t.schema() for t in tools_for(ctx.principal.role)]
        try:
            for _ in range(self.s.agent_max_steps):
                reply = self.llm.chat(msgs, tools=schemas, tier="primary")
                if not reply.tool_calls:
                    return {"llm_messages": msgs + [reply.as_message()], "draft": (reply.content or "").strip(), "status": "ok"}
                msgs = msgs + [reply.as_message()]
                for call in reply.tool_calls:
                    if "__invalid_json__" in call.arguments:
                        content = json.dumps({"ok": False, "error": "Arguments were not valid JSON."})
                    else:
                        res = execute(ctx, call.name, call.arguments)
                        content = json.dumps({"ok": res.ok, "data": res.data} if res.ok else {"ok": False, "error": res.error},
                                             default=str)
                    if len(content) > MAX_TOOL_CONTENT:
                        content = content[:MAX_TOOL_CONTENT] + '..."(truncated)"'
                    msgs.append({"role": "tool", "tool_call_id": call.id, "content": content})
            return {"llm_messages": msgs, "status": "ok",
                    "draft": "I wasn't able to complete this request within the allowed number of steps. "
                             "Please try a more specific question."}
        except LLMUnavailable:
            return self._degraded(s)

    def _degraded(self, s: TurnState) -> dict:
        """The LLM is down: answer policy questions with raw retrieved excerpts, otherwise fail clearly."""
        ctx = s["ctx"]
        ctx.trace.error_code = "llm_unavailable"
        if _POLICY_HINT.search(s["message"]):
            res = execute(ctx, "search_policy", {"query": s["message"][:300]})
            if res.ok and res.data.get("results"):
                parts = [f"{r['ref']} {r['section']} (v{r['version']}):\n{r['text'][:500]}" for r in res.data["results"][:2]]
                return {"status": "degraded", "draft": "The AI assistant is temporarily unavailable, so I can't compose a full answer. "
                        "These are the most relevant policy excerpts:\n\n" + "\n\n".join(parts)}
        return {"status": "degraded", "draft": "The AI assistant is temporarily unavailable. Nothing was changed. Please try again "
                "shortly, or use the HR portal for urgent requests."}

    def _verify(self, s: TurnState) -> dict:
        ctx, draft = s["ctx"], s["draft"]
        context_text = s["message"] + "\n" + "\n".join(m["content"] for m in s["history"]) + "\n" + json.dumps(s["memory"], default=str) \
            + f"\n{ctx.today.isoformat()} 60% 90-day 3 working days 5 working days"
        rep = check_grounding(draft, ctx.evidence, context_text, set(ctx.citations))
        problems = rep.feedback()
        if _SUCCESS_CLAIM.search(draft):
            problems = (problems + "; " if problems else "") + ("the answer claims an action was completed, but actions only "
                        "happen after the user confirms; describe it as a proposal awaiting confirmation")
        if not problems:
            return {"status": "verified"}
        ctx.trace.audit("grounding_issue", {"problems": problems, "attempt": s["retries"] + 1})
        if s["retries"] < 1:
            msgs = s["llm_messages"] + [{"role": "user", "content":
                   f"[Automatic answer check, not from the user] Fix your previous answer: {problems}. Use tools again if needed, "
                   "use only figures that appear in tool results, and do not mention this check."}]
            return {"status": "regenerate", "retries": s["retries"] + 1, "llm_messages": msgs}
        fixed = draft
        if _SUCCESS_CLAIM.search(fixed) and ctx.proposed:
            fixed = proposal_text(ctx.proposed.model_dump(mode="json"))
        for ref in rep.invalid_citations:
            fixed = fixed.replace(ref, "")
        if rep.ungrounded_numbers:
            fixed += ("\n\n_Note: some figures in this answer could not be verified against source data ("
                      + ", ".join(rep.ungrounded_numbers) + "). Please double-check them._")
        return {"status": "verified", "draft": fixed, "issues": [problems]}

    def _finalize(self, s: TurnState) -> dict:
        ctx = s["ctx"]
        answer = s.get("draft") or "I couldn't produce an answer."
        refs = list(dict.fromkeys(re.findall(r"\[P\d+\]", answer)))
        cites: list[Citation] = [ctx.citations[r] for r in refs if r in ctx.citations]
        searched_empty = any(e["tool"] == "search_policy" and not e["data"].get("results") for e in ctx.evidence)
        status = s.get("status", "ok")
        if status in ("blocked", "unsupported", "degraded", "clarify") or (searched_empty and not cites):
            confidence = "abstained" if status != "degraded" else "partial"
        elif s.get("issues"):
            confidence = "partial"
        else:
            confidence = "grounded"
        pending = ctx.proposed
        if pending is not None:
            prop = open_proposal(ctx.principal)
            if prop is None or str(prop["id"]) != str(pending.action_id):
                pending = None
        if status == "verified":
            status = "ok"
        sources = sorted({e["tool"] for e in ctx.evidence})
        return {"status": status, "final": AgentResponse(answer=answer, citations=cites, data_sources=sources,
                                                         pending_action=pending, confidence=confidence,
                                                         trace_id=ctx.trace.trace_id)}


# ---------------------------------------------------------------------- helpers
def heuristic_route(message: str) -> RouteDecision:
    m = message.lower()
    intents: list[Intent] = []
    if _POLICY_HINT.search(m):
        intents.append(Intent.POLICY)
    if re.search(r"\b(apply|book|submit|approve|reject)\b", m):
        intents.append(Intent.ACTION)
    if re.search(r"\b(my team|reports|team)\b", m):
        intents.append(Intent.TEAM_DATA)
    if re.search(r"\b(how many|count|department|vacan|trend|anomal|forecast)\b", m):
        intents.append(Intent.ANALYTICS)
    return RouteDecision(intents=intents or [Intent.PERSONAL_DATA], confidence=0.3)


def proposal_text(pa: dict) -> str:
    p = pa["payload"]
    if pa["type"] == "submit_leave":
        return (f"Proposed leave application (not submitted yet): {p['leave_type']} from {p['start_date']} to {p['end_date']} "
                f"({p['days']} working day(s)); balance {p['balance_before']} → {p['balance_after']}; approver {p['approver_name']}. "
                "Please confirm or cancel.")
    return (f"Proposed decision (not applied yet): {p['decision']} request {p['request_id']} from {p['requester_name']}. "
            "Please confirm or cancel.")


def action_outcome_text(prop: dict, out: Any) -> str:
    if out.status == "executed":
        if prop["action_type"] == "submit_leave":
            p = prop["payload"]
            return (f"Done. Leave request **{out.request_id}** has been created with status **{out.request_status}** "
                    f"({p['leave_type']}, {p['start_date']} to {p['end_date']}). It is now awaiting approval by {p['approver_name']}.")
        return f"Done. Request **{out.request_id}** is now **{out.request_status}**."
    if out.status == "already_executed":
        return f"This was already completed earlier: request **{out.request_id}** is **{out.request_status}**. No duplicate was created."
    return f"The action could not be completed: {out.error} Nothing was changed."
