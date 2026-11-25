# Technical design note

## Model selection
- **LLM: selectable with the `LLM_PROVIDER` flag**: `openai` (defaults `gpt-5.5` primary, `gpt-5.4-mini` fast) or `grok`
  (defaults `grok-4` / `grok-3-mini`). Both go through the OpenAI Chat Completions API with one client (`ChatLLM`). The
  primary model drives tool selection and multi-step reasoning. The fast model handles intent routing and text-to-SQL,
  where latency and cost matter more. On OpenAI, the fast model's `reasoning_effort` (default `low`) is sent only on
  tool-free calls, because Chat Completions rejects it alongside function tools for some models
  than depth. Both IDs are env-configurable, and calls go through a small `LLM` protocol (`app/agent/llm.py`), so other
  providers or a local model can be swapped in.
- **Fallback**: transient errors (connection, timeout, 429, 5xx) are retried with backoff, then the other model is tried.
  If both fail, the turn degrades deterministically: policy questions return cited excerpts from retrieval, and other
  requests get a clear "unavailable, nothing changed" reply.
- **Embeddings / reranker**: local ONNX models (bge-small, MiniLM cross-encoder). Zero cost, deterministic, and the
  policy corpus is small and in English, so a larger model buys little.

## RAG
- **Ingestion** (`app/rag/ingest.py`): Markdown with YAML front matter (doc_key, version, effective_from/to). The sha256
  makes re-ingestion idempotent. A new version is added beside the old one, and `is_current` is recomputed from the
  effective dates, so documents update without any retraining.
- **Chunking**: one chunk per heading section, with the heading path kept ("Leave Policy > 4. Earned Leave > 4.4 Carry
  forward"); long sections split on paragraphs with a one-paragraph overlap. Policies are short, structured documents,
  and section chunks give precise citations and strong lexical signals.
- **Retrieval**: pgvector cosine (top 20) plus Postgres full-text (top 20), fused with Reciprocal Rank Fusion, then a
  cross-encoder reranks the top 10 and the top 5 are kept. Results are restricted to the current version, or to the
  version in force at `as_of` for historical questions.
- **Grounding / abstention**: chunks below a rerank score of −4.0 are dropped. An empty result tells the model to say the
  policies don't cover the topic. Answers cite `[P#]` labels, which are mapped back to chunk, document and version, and
  citations that weren't retrieved are rejected.
- Measured on the five example queries in the brief: the correct section ranks first in every case (`tests/test_tools_security.py::test_retrieval_top1`).

## Structured data
- **Predefined tools first** (`workforce_stats`, balances, requests, availability): they are exact, scoped and cheap.
  **Text-to-SQL** (`run_sql_analytics`) is a fallback for ad-hoc analytics. It runs on five scoped views, validated by
  sqlglot, as the `pl_readonly` role, in a read-only transaction.
- Scope lives in the database: views filter on `hr.visible_employee_ids()`, which reads `app.employee_id` and `app.role`.
  Those are set per transaction from the verified principal, never from the LLM.
- Leave rules (`app/core/leave_rules.py`) are code. A unit test asserts that they match the policy text.

## Orchestration
A LangGraph state graph with one ReAct-style agent over role-filtered tools. Tools are selected dynamically: the model
chooses from typed tool schemas based on intent, rather than from keyword routes. The router (fast model) adds intent
labels for telemetry and short-circuits unsupported or ambiguous requests. A verification node checks grounding and
allows one regeneration. Multi-step requests such as the 6.5 review prioritisation combine `assess_pending_requests`
(deterministic factors: start date, waiting time, coverage, overlaps, balance, notice; no personal attributes),
`team_availability` and `search_policy`.

## Memory
- `agent.messages`: the last 6 turns as text, replayed to the model. Raw tool outputs are not replayed.
- `agent.session_state.entities`: structured pointers (last request ids, last date range and filter) that resolve
  "these" and "that period".
- Isolation and revalidation: every request checks session ownership, and the role is re-read from the database. Memory
  holds only ids, and every use goes back through authorized tools.

## Guardrails
See the controls table in [ARCHITECTURE.md](ARCHITECTURE.md#security-controls-none-rely-on-the-prompt). The principle is
that the prompt asks for good behaviour, while code and the database enforce it. Even a fully jailbroken model can only
call tools for its own role, inside the caller's scope, and can only *propose* actions.

## Evaluation
`eval/golden.yaml` has 42 cases (policy 8, structured 8, multi-step 7, tools/actions 6, security 9, ambiguous 4).
Checks are deterministic. Expected numbers and names come from SQL over live data at check time, so they can't go stale.
Metrics are pass rate, retrieval accuracy, citation correctness, answer correctness, tool-selection accuracy, action
success, unauthorized-access prevention, latency (mean/p95), tokens and estimated cost. The run reseeds a deterministic
database (`APP_TODAY=2026-11-25`, seed 42). An LLM judge was left out deliberately: deterministic assertions are
reproducible and cheaper, and grading free-text quality is the main gap (see limitations).

## Workforce intelligence (6.7)
- **Anomaly detection**: robust z-score on absence rate (median/MAD, z ≥ 3.5), leave concentration (≥ 40% of a team in a
  week) and rule-based inconsistencies. Each seeded anomaly is detected (test-covered).
- **Availability forecast**: scheduled leave (fact) plus predicted unplanned absence (weekday-seasonal rate). A rolling
  4-week backtest compares it with a flat-rate baseline (MAE in people per day). The model is used only if it beats the
  baseline. On the synthetic data it does *not*, because absences are generated without a weekday effect, so the tool
  honestly falls back to the baseline.

## Limitations
- Demo authentication (pick a seeded identity). Production would use SSO/OIDC, with roles from the HRIS.
- The rule-based injection guard can be evaded by paraphrase. That is acceptable only because authorization does not
  depend on it. Production would add a classifier model.
- The grounding check tolerates the trivial numbers 0–2, and percentages that are ratios of grounded counts.
- History handling is a fixed window (6 turns), with no summarisation of older turns.
- Escalation for stale approvals (> 3 working days) is flagged in recommendations, not automated.
- Single-region, single-tenant. There is no row-level-security policy on base tables; scope is enforced through views
  and the role.

## Production improvements
SSO and an HRIS sync; a learned injection classifier; LLM-judge or human review for answer quality; semantic caching of
policy answers; streaming responses; OpenTelemetry export (Langfuse/Grafana); background re-ingestion on document
change; RLS on base tables; per-tenant keys; rate limiting; a notification workflow for approvals and escalations.
