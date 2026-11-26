# People Lens: Enterprise Workforce Intelligence & Agentic AI Assistant

An agentic HR assistant that answers policy questions with cited evidence (hybrid RAG), queries live workforce data within
each user's permissions, reasons over multiple sources, and carries out leave actions with explicit confirmation. All
guardrails are enforced in code and in the database, not in the prompt.

- Architecture and diagram: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Design note (models, RAG, orchestration, memory, guardrails, evaluation, limitations): [docs/DESIGN_NOTE.md](docs/DESIGN_NOTE.md)
- Data model and tool registry: [docs/DATA_MODEL.md](docs/DATA_MODEL.md)

## Quick start (local)

Requirements: Python 3.12+, Docker, and an OpenAI or xAI (Grok) API key.

Choose the LLM with `LLM_PROVIDER=openai` or `LLM_PROVIDER=grok` in `.env`. Each provider has its own key and model
settings (`OPENAI_*`, `XAI_API_KEY` / `GROK_*`); switching is a one-line change plus a restart.

```bash
cp .env.example .env        # set LLM_PROVIDER and that provider's API key
scripts/run.sh              # Postgres + backend (:8000) + frontend (:8501); Ctrl+C to stop
scripts/run.sh --reseed     # same, but rebuild the synthetic data and policy index first
```

`run.sh` creates the virtualenv if it's missing, starts Docker and the Postgres container, seeds and ingests on first
run, waits for health checks, and writes logs to `.logs/`. Ports can be changed with `API_PORT`, `UI_PORT` and `PG_PORT`.

Manual steps, if you prefer:

```bash
cp .env.example .env                       # set LLM_PROVIDER and its API key (and optionally the model IDs)
docker run -d --name pl-postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=peoplelens \
  -p 55432:5432 pgvector/pgvector:pg16
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m scripts.seed_data      # schema + deterministic synthetic data
.venv/bin/python -m app.rag.ingest         # embed and index the policy documents (downloads ~100 MB of local models once)

.venv/bin/uvicorn app.api.main:app --port 8000                 # API, docs at http://localhost:8000/docs
API_URL=http://127.0.0.1:8000 .venv/bin/uvicorn ui.serve:app --port 8501  # UI at http://localhost:8501
```

Or run everything in Docker: `docker compose up --build` (the API is at :8000 and the UI at :8501).

## The app

| Who | Pages |
|---|---|
| Everyone | **Home**: the AI agent card (chat, plus Suggestions, Pending, Actions and Insights tabs), a leave balance widget with **Request leave**, today's check-in, and recent requests. **My leave**: balances, requests with status, notifications. Check-in, notifications and Ask AI are in the top bar. |
| Managers (and any approver) | **Approvals**: pending requests with **Approve** and **Reject** (optional reason), plus an approvals widget on Home |
| HR administrators | **All leaves**: org metrics and all requests (managers included) with search and filters, plus a **By employee** view of balances and history |

New requests notify the approver, and decisions notify the employee. Every action re-checks permissions on the server.

## MCP server (use People Lens from any MCP-compatible client)

`app/mcp_server.py` exposes `whoami`, `get_leave_balance`, `apply_leave`, `my_leave_requests`, `check_leave_eligibility`,
`search_policy`, `notifications`, `check_in`, `check_out`, `pending_approvals` and `decide_leave_request`.
`apply_leave` is interactive. It shows the available balance and asks the employee for the leave type and dates
(MCP elicitation), validates them against policy, asks for confirmation, then creates the approval request, which
appears on the manager's Approvals page and in HR's view. The server is a thin client of the API: start the backend
first. It never touches the database directly.

Configure your MCP-compatible client to launch `python -m app.mcp_server` over stdio, and provide the environment
variables below. Use the client's own MCP configuration format; the server is not tied to a particular model provider.
`PEOPLELENS_EMPLOYEE_ID` signs in as a seeded employee (demo). Use `PEOPLELENS_TOKEN` with a JWT from `/auth/login` instead
to pass an existing session. `python -m app.mcp_server --http --port 8765` serves streamable HTTP. Clients without
elicitation support get the same flow in two steps: the tool returns what's missing or a summary, and submits only with
`confirm=true`.

## Using it
Sign in as any seeded person (`Arjun Mehta`: employee, `Priya Sharma`: manager, `Neha Kapoor`: HR admin). Leave
proposals in the chat show **Confirm / Cancel**; replying "yes" or "no" also works. Traces, latency and token usage are
kept out of the UI: they're available at `/traces/{id}` and `/metrics/summary` (HR) in the API.

"Today" is pinned to `APP_TODAY=2026-11-25` so that the data and relative dates ("next week" = 30 Nov to 4 Dec) are
reproducible. Clear it to use the real date (then reseed).

## Tests and evaluation

```bash
.venv/bin/python -m pytest                 # 103 tests; fake LLM, real Postgres, MCP in-process; no API key needed
.venv/bin/python -m eval.run_eval          # 42 golden cases against the configured LLM; reseeds the DB first
```

The evaluation writes `eval/results/latest.md` (metrics and a table of every case and answer), `results_<ts>.csv` and
`metrics_<ts>.json`, and stores the run in `ops.eval_runs`.

## What is implemented

| Requirement | Status | Where |
|---|---|---|
| 3 seeded roles, ≥200 employees, ≥500 leave records, 3 months of attendance, direct/indirect hierarchy | ✅ | `scripts/seed_data.py` |
| ≥5 policies, versioned (leave policy v1 → v2), plus a document with an injected instruction | ✅ | `data/policies/` |
| RAG: section chunking, hybrid vector + FTS, RRF, rerank, abstention, version-aware, citations | ✅ | `app/rag/` |
| NL structured queries: predefined scoped tools plus guarded text-to-SQL | ✅ | `app/tools/hr_tools.py`, `analytics.py` |
| Routing and dynamic tool selection, multi-source and multi-step reasoning | ✅ | `app/agent/graph.py` |
| Actions: submit leave, approve/reject, with confirmation, idempotency and no self-approval | ✅ | `app/tools/actions.py` |
| Multi-turn memory, session isolation, permission revalidation | ✅ | `app/agent/memory.py`, `app/api/main.py` |
| Guardrails outside the LLM | ✅ | `app/security/` |
| Workforce intelligence: anomaly detection and forecasting with a backtested baseline | ✅ | `app/intelligence/` |
| Error handling: LLM down (degraded), DB retry, tool errors, no evidence, duplicate confirms | ✅ | tests in `tests/test_orchestrator.py` |
| Tracing, token, latency and cost monitoring | ✅ | `app/core/observability.py`, `/traces/{id}`, `/metrics/summary` |
| Golden set ≥30 with automated metrics | ✅ 42 cases | `eval/` |
| Docker deployment, model fallback | ✅ | `docker-compose.yml`, `app/agent/llm.py` |
| MCP server: balance, interactive apply (elicitation), approvals, check-in, policy | ✅ | `app/mcp_server.py` |
| Dashboards: employee, manager approvals, HR all leaves; notifications; check-in/out | ✅ | `ui/web/` (HTML/CSS/JS), `app/api/portal.py` |
| Streaming, semantic caching, LLM-judge evaluation | ❌ not implemented | see the design note |

## Repository layout
```
app/api        FastAPI: auth, chat, actions, portal (dashboards, approvals, HR, notifications, attendance)
app/mcp_server.py  MCP server (thin authenticated client of the API)
app/services   notifications, attendance
app/agent      graph.py (LangGraph), llm.py (OpenAI/Grok client, chosen by LLM_PROVIDER), prompts.py, memory.py, models.py
app/tools      registry/executor, HR read tools, analytics, two-phase actions
app/security   authz, input/output guards, SQL validator
app/rag        chunker, ingest, retriever, local embeddings/reranker
app/core       date resolution, leave rules engine, observability
app/intelligence  anomalies, forecast
app/db         schema.sql and the connection pool
data/policies  synthetic policy documents
scripts        seed_data.py
eval           golden.yaml, run_eval.py, results/
tests          pytest suite
ui             web app: ui/web (HTML/CSS/JS, no build step) served by ui/serve.py
```
