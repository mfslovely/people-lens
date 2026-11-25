# People Lens: Data Model

The executable DDL is [`app/db/schema.sql`](../app/db/schema.sql); the Pydantic models are in [`app/agent/models.py`](../app/agent/models.py).

## Schemas

| Schema | Tables / views | Purpose |
|---|---|---|
| `hr` | `departments` (with parent), `positions` (filled/vacant/frozen), `employees` (manager_id self-FK), `app_users` (role), `leave_types`, `leave_balances` (generated `available`, CHECK ≥ 0), `leave_requests` (EXCLUDE overlap, UNIQUE idempotency_key, approver ≠ employee), `approval_history`, `attendance`, `holidays` | Synthetic HR system of record |
| `hr` (views) | `v_reporting_tree` (recursive, direct and indirect), `v_employees_scoped`, `v_leave_scoped`, `v_balances_scoped`, `v_attendance_scoped`, `v_positions` | Scoped read surface; the only objects `pl_readonly` can read |
| `kb` | `documents` → `document_versions` (effective_from/to, is_current, sha256) → `chunks` (section_path, `vector(384)` HNSW, generated `tsvector` GIN, flagged_injection) | Versioned knowledge base |
| `agent` | `sessions`, `messages`, `session_state` (entities JSONB), `pending_actions` (two-phase actions, status machine, expiry, UNIQUE idempotency_key) | Conversation memory and action control |
| `ops` | `traces`, `tool_calls`, `audit_log`, `eval_runs`, `eval_results` | Observability and evaluation history |

Database-level invariants: no overlapping active leave for an employee (GiST EXCLUDE), no negative balance (CHECK), no
self-approval (CHECK), at most one current version per document (partial unique index), and no duplicate execution
(UNIQUE idempotency key).

## Synthetic dataset (`scripts/seed_data.py`, seed 42, today pinned by `APP_TODAY`)
220 employees, 8 departments (2 sub-departments), 18 people with reports (including 12 managers), 2 HR admins, 592 leave
requests, about 13.5k attendance rows (92 days), 238 positions (20 vacant, 3 frozen), and the holidays from the policy.
Seeded scenarios: three pending requests for the IT manager's team next week; an IT head on leave today (tests
escalation); a probationary employee; a high-absence employee; a leave-concentration week in Operations; and
attendance/leave inconsistencies.

| Persona | id | Role | Notes |
|---|---|---|---|
| Arjun Mehta | 20 | employee | IT, reports to Priya |
| Rohan Verma | 21 | employee | joined 30 days ago (probation) |
| Kavya Nair | 30 | employee | Sales (outside Priya's team) |
| Priya Sharma | 8 | manager | IT; 20 people in her tree (direct and indirect via Sana Iqbal) |
| Vikram Singh | 9 | manager | Sales |
| Neha Kapoor | 13 | hr_admin | |

## Agent models
`Principal` (frozen; from JWT plus DB), `RouteDecision`, `GuardVerdict`, `Citation` (ref, chunk, doc, version, section,
score), `ToolResult`, `LeaveProposal`, `DecisionPayload`, `ProposedAction`, `ActionOutcome` (the only source of success
messages), `ReviewRecommendation`, `AgentResponse` (answer, citations, sources, pending_action, confidence, trace).

## Tool registry

| Tool | Employee | Manager | HR | Notes |
|---|---|---|---|---|
| `search_policy(query, as_of?)` | ✓ | ✓ | ✓ | hybrid RAG; `[P#]` labels |
| `resolve_dates(expression)` | ✓ | ✓ | ✓ | deterministic date arithmetic |
| `get_employee_profile(employee?)`, `get_reporting_chain(employee?)` | self | tree | org | |
| `get_leave_balance(employee?, leave_type?)` | self | tree | org | |
| `list_leave_requests(scope, status, window, …)` | mine | + team, assigned_to_me | + organization | |
| `check_leave_eligibility(type, start, end, half_day)` | ✓ | ✓ | ✓ | rules engine and approver routing |
| `get_team`, `team_availability`, `assess_pending_requests` | – | ✓ | ✓ | coverage, overlaps, review order |
| `workforce_stats(metric, …)`, `run_sql_analytics(question)` | – | tree | org | scoped views |
| `detect_anomalies`, `forecast_availability` | – | tree | org | statistics; the LLM explains |
| `propose_leave(…)` | ✓ | ✓ | ✓ | proposal only |
| `propose_decision(request_id, approve/reject)` | – | assigned & not own | assigned & not own | proposal only |
| *confirm / cancel* | user only (button, API or "yes"), **never callable by the LLM** | | | |
