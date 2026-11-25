"""Reproducible evaluation harness.

Usage:
  python -m eval.run_eval                 # reseeds the DB and re-ingests policies, then runs every case against Grok
  python -m eval.run_eval --no-reseed     # reuse the current DB
  python -m eval.run_eval --only SEC-01,POL-02

Outputs eval/results/latest.md, results_<ts>.csv and metrics_<ts>.json, plus rows in ops.eval_runs / ops.eval_results.

Metrics (all deterministic, no LLM judge):
  pass_rate                     cases passing every check / total
  per-category pass rate
  retrieval_accuracy            cases with cite_doc where that doc appears among the chunks retrieved by search_policy
  citation_correctness          cases with cite_doc where it is cited, and every cited chunk was actually retrieved
  answer_correctness            cases with content/ground-truth checks (contains / sql_*) where those checks pass
  tool_selection_accuracy       cases with tool expectations where tools_all / tools_any / tools_none all hold
  action_success                cases with confirm=true where the action executed and the DB check holds
  unauthorized_access_prevention  security-category pass rate
  latency (mean / p95), tokens and estimated cost from traces
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import memory  # noqa: E402
from app.agent.graph import Orchestrator  # noqa: E402
from app.agent.models import Principal, Role  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.observability import setup_logging  # noqa: E402
from app.db import tx  # noqa: E402
from app.tools.actions import confirm_action  # noqa: E402

USERS = {"arjun": 20, "rohan": 21, "kavya": 30, "priya": 8, "vikram": 9, "neha": 13, "sana": 22}
RESULTS = ROOT / "eval/results"


def principal(user: str) -> Principal:
    emp = USERS[user]
    with tx() as conn:
        u = conn.execute("SELECT id, role FROM hr.app_users WHERE employee_id=%s", (emp,)).fetchone()
    sid = memory.create_session(u["id"], u["role"])
    return Principal(user_id=u["id"], employee_id=emp, role=Role(u["role"]), session_id=sid)


def scalar(sql: str):
    with tx() as conn:
        row = conn.execute(sql).fetchone()
    return list(row.values())[0] if row else None


def column(sql: str) -> list[str]:
    with tx() as conn:
        return [str(list(r.values())[0]) for r in conn.execute(sql).fetchall()]


def fmt_num(v) -> list[str]:
    f = float(v)
    return [str(int(f))] if f.is_integer() else [f"{f:g}", f"{f:.1f}"]


def chunk_docs(ids: list[int]) -> set[str]:
    if not ids:
        return set()
    with tx() as conn:
        return {r["doc_key"] for r in conn.execute(
            """SELECT DISTINCT d.doc_key FROM kb.chunks c JOIN kb.document_versions v ON v.id=c.version_id
               JOIN kb.documents d ON d.id=v.document_id WHERE c.id = ANY(%s)""", (ids,)).fetchall()}


def run_case(orch: Orchestrator, case: dict) -> dict:
    exp = case.get("expect", {})
    vars_ = {k: scalar(v) for k, v in (case.get("vars") or {}).items()}

    def sub(s: str) -> str:
        for k, v in vars_.items():
            s = s.replace("{" + k + "}", str(v))
        return s

    p = principal(case["user"])
    t0 = time.perf_counter()
    resp = None
    tokens = 0
    for turn in case["turns"]:
        resp = orch.run(p, sub(turn))
        tokens += (resp.trace or {}).get("input_tokens", 0) + (resp.trace or {}).get("output_tokens", 0)
    latency = int((time.perf_counter() - t0) * 1000)
    assert resp is not None
    answer = resp.answer
    low = answer.lower()
    trace = resp.trace or {}
    tools = [t["tool"] for t in trace.get("tools", [])]
    retrieved = [c for t in trace.get("tools", []) if t["tool"] == "search_policy" for c in t.get("chunks") or []]
    checks: dict[str, bool] = {}
    groups = {"tool": [], "answer": [], "citation": [], "retrieval": [], "action": []}

    def chk(name: str, ok: bool, group: str | None = None):
        checks[name] = bool(ok)
        if group:
            groups[group].append(bool(ok))

    if "tools_all" in exp:
        chk("tools_all", all(t in tools for t in exp["tools_all"]), "tool")
    if "tools_any" in exp:
        chk("tools_any", any(t in tools for t in exp["tools_any"]), "tool")
    if "tools_none" in exp:
        chk("tools_none", not any(t in tools for t in exp["tools_none"]), "tool")
    if "contains_all" in exp:
        chk("contains_all", all(sub(s).lower() in low for s in exp["contains_all"]), "answer")
    if "contains_any" in exp:
        chk("contains_any", any(sub(s).lower() in low for s in exp["contains_any"]), "answer")
    if "not_contains" in exp:
        chk("not_contains", not any(s.lower() in low for s in exp["not_contains"]), "answer")
    if "sql_number" in exp:
        v = scalar(sub(exp["sql_number"]))
        nums = set(re.findall(r"\d+(?:\.\d+)?", answer))
        chk("sql_number", any(n in nums for n in fmt_num(v)), "answer")
        checks["sql_number_expected"] = fmt_num(v)[0]  # type: ignore[assignment]
    if "sql_any" in exp:
        vals = column(sub(exp["sql_any"]))
        chk("sql_any", any(v.lower() in low for v in vals), "answer")
    if "cite_doc" in exp:
        cited = {c.doc_key for c in resp.citations}
        chk("cite_doc", exp["cite_doc"] in cited and {c.chunk_id for c in resp.citations} <= set(retrieved), "citation")
        chk("retrieved_doc", exp["cite_doc"] in chunk_docs(retrieved), "retrieval")
    if exp.get("abstain"):
        chk("abstain", resp.confidence == "abstained" and not resp.citations, "answer")
    if exp.get("blocked"):
        chk("blocked", trace.get("status") == "blocked" and trace.get("llm_calls", 0) == 0)
    if "pending_action" in exp:
        want = exp["pending_action"]
        got = resp.pending_action.type if resp.pending_action else "none"
        chk("pending_action", got == want, "tool")
    if exp.get("confirm"):
        pa = resp.pending_action
        ok = False
        if pa is not None:
            r2 = orch.run(p, "yes")
            ok = bool(re.search(r"LR-\d{4}-\d{6}", r2.answer)) and "created" in r2.answer
            answer += "\n\n[confirm → ] " + r2.answer
            if exp.get("confirm_twice"):
                again = confirm_action(p, str(pa.action_id))
                chk("confirm_twice", again.status == "already_executed", "action")
        chk("confirm", ok, "action")
    if "db_check" in exp:
        chk("db_check", bool(scalar(sub(exp["db_check"]))), "action" if exp.get("confirm") else None)

    bool_checks = {k: v for k, v in checks.items() if isinstance(v, bool)}
    return {"id": case["id"], "category": case["category"], "user": case["user"],
            "role": p.role.value, "query": " ⏎ ".join(sub(t) for t in case["turns"]),
            "expected": json.dumps({k: (sub(v) if isinstance(v, str) else v) for k, v in exp.items()}, default=str),
            "actual_output": answer, "tools": tools, "confidence": resp.confidence,
            "passed": all(bool_checks.values()), "checks": checks, "groups": groups,
            "latency_ms": latency, "tokens": tokens, "cost_usd": trace.get("cost_usd", 0), "trace_id": resp.trace_id}


def rate(xs: list[bool]) -> float | None:
    return round(100 * sum(xs) / len(xs), 1) if xs else None


def summarize(results: list[dict]) -> dict:
    cats = sorted({r["category"] for r in results})
    lat = [r["latency_ms"] for r in results]
    g = lambda name: [all(r["groups"][name]) for r in results if r["groups"][name]]  # noqa: E731
    return {
        "cases": len(results), "passed": sum(r["passed"] for r in results), "pass_rate": rate([r["passed"] for r in results]),
        "by_category": {c: f"{sum(r['passed'] for r in results if r['category']==c)}/{sum(r['category']==c for r in results)}" for c in cats},
        "retrieval_accuracy": rate(g("retrieval")), "citation_correctness": rate(g("citation")),
        "answer_correctness": rate(g("answer")), "tool_selection_accuracy": rate(g("tool")),
        "action_success": rate(g("action")),
        "unauthorized_access_prevention": rate([r["passed"] for r in results if r["category"] == "security"]),
        "latency_ms_mean": int(statistics.mean(lat)) if lat else None,
        "latency_ms_p95": sorted(lat)[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)] if lat else None,
        "tokens_total": sum(r["tokens"] for r in results), "est_cost_usd": round(sum(r["cost_usd"] or 0 for r in results), 4),
    }


def write_reports(results: list[dict], metrics: dict, model: str) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(RESULTS / f"results_{ts}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "category", "user", "role", "query", "expected", "actual_output", "tools",
                                          "confidence", "passed", "checks", "latency_ms", "tokens", "trace_id"])
        w.writeheader()
        for r in results:
            w.writerow({k: (json.dumps(r[k], default=str) if k in ("tools", "checks") else r[k]) for k in w.fieldnames})
    (RESULTS / f"metrics_{ts}.json").write_text(json.dumps({"model": model, **metrics}, indent=2))
    lines = [f"# Evaluation results ({ts})", "", f"Model: `{model}` · Today pinned to {get_settings().today}", "",
             "## Metrics", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in metrics.items() if k != "by_category"]
    lines += ["", "## By category", "", "| Category | Passed |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in metrics["by_category"].items()]
    lines += ["", "## Cases", "", "| ID | Role | Query | Pass | Failed checks | Tools | Latency ms |", "|---|---|---|---|---|---|---|"]
    for r in results:
        failed = [k for k, v in r["checks"].items() if v is False]
        q = r["query"].replace("|", "/")[:90]
        lines.append(f"| {r['id']} | {r['role']} | {q} | {'✅' if r['passed'] else '❌'} | {', '.join(failed)} | "
                     f"{', '.join(dict.fromkeys(r['tools']))} | {r['latency_ms']} |")
    lines += ["", "## Answers", ""]
    for r in results:
        lines += [f"### {r['id']} {'✅' if r['passed'] else '❌'}", f"**{r['role']}**: {r['query']}", "",
                  "```", r["actual_output"][:1500], "```", ""]
    (RESULTS / "latest.md").write_text("\n".join(lines))
    with tx() as conn:
        run_id = conn.execute("INSERT INTO ops.eval_runs (model, metrics) VALUES (%s, %s::jsonb) RETURNING id",
                              (model, json.dumps(metrics))).fetchone()["id"]
        for r in results:
            conn.execute("""INSERT INTO ops.eval_results (run_id, case_id, category, passed, actual_output, checks, latency_ms, tokens)
                            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
                         (run_id, r["id"], r["category"], r["passed"], r["actual_output"], json.dumps(r["checks"], default=str),
                          r["latency_ms"], r["tokens"]))
    return RESULTS / "latest.md"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-reseed", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    setup_logging(level=40)
    s = get_settings()
    if not s.llm.configured:
        sys.exit(f"No API key for LLM_PROVIDER={s.llm.provider} in .env. The evaluation runs against the real model.")
    if not args.no_reseed:
        subprocess.run([sys.executable, "-m", "scripts.seed_data"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-m", "app.rag.ingest"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    cases = yaml.safe_load((ROOT / "eval/golden.yaml").read_text())
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    orch = Orchestrator()
    results = []
    for c in cases:
        try:
            r = run_case(orch, c)
        except Exception as e:  # noqa: BLE001
            r = {"id": c["id"], "category": c["category"], "user": c["user"], "role": "", "query": " ⏎ ".join(c["turns"]),
                 "expected": json.dumps(c.get("expect")), "actual_output": f"HARNESS ERROR: {e!r}", "tools": [],
                 "confidence": "", "passed": False, "checks": {"harness": False}, "groups": {k: [] for k in
                 ("tool", "answer", "citation", "retrieval", "action")}, "latency_ms": 0, "tokens": 0, "cost_usd": 0, "trace_id": ""}
        results.append(r)
        failed = [k for k, v in r["checks"].items() if v is False]
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['id']:9} {r['latency_ms']:>6}ms  tools={list(dict.fromkeys(r['tools']))}"
              + (f"  failed={failed}" if failed else ""), flush=True)
    metrics = summarize(results)
    path = write_reports(results, metrics, f"{s.llm.provider}: {s.llm.primary_model} (+{s.llm.fast_model})")
    print(json.dumps(metrics, indent=2))
    print(f"report: {path}")


if __name__ == "__main__":
    main()
