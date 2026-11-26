# Submission deliverables

This repository contains the complete People Lens assignment submission. All workforce records and policies are
synthetic. No external HR system or confidential company data is used.

## Section 10 checklist

| Deliverable | Location | Notes |
|---|---|---|
| Source repository and meaningful history | Repository root | Python application, web UI, tests, Docker and local runner |
| Installation and execution instructions | [README](../README.md) | One-command local runner, manual setup and Docker setup |
| AI system architecture diagram | [Architecture](ARCHITECTURE.md#system-diagram) | Mermaid system, request lifecycle and channel diagrams |
| Synthetic datasets and initialization | [Seed generator](../scripts/seed_data.py), [schema](../app/db/schema.sql) | Deterministic 220-person workforce, reporting graph, leave, attendance and audit history |
| Policy documents | [Policy directory](../data/policies) | Seven synthetic documents including versioned leave policy |
| Prompts, tools and orchestration | [prompts](../app/agent/prompts.py), [tools](../app/tools), [graph](../app/agent/graph.py) | Role-filtered tools and LangGraph orchestration |
| Golden dataset | [golden.yaml](../eval/golden.yaml) | 42 cases across all required categories |
| Evaluation script | [run_eval.py](../eval/run_eval.py) | Deterministic clock, real configured model and reproducible checks |
| Actual evaluation results | [latest report](../eval/results/latest.md), [metrics JSON](../eval/results/metrics_latest.json) | 42/42 passing on the recorded run |
| Credential-free configuration | [.env.example](../.env.example) | Empty provider keys and replaceable demo JWT secret |
| Technical design note | [Design note](DESIGN_NOTE.md) | Models, RAG, orchestration, memory, guardrails, evaluation, limitations and production roadmap |
| Demonstration video | Optional, not included | The assignment allows a video of up to five minutes |

## Recorded verification

- Automated tests: 118/118 passed.
- Golden evaluation: 42/42 passed.
- Retrieval accuracy: 100%.
- Citation correctness: 100%.
- Answer correctness: 100%.
- Tool-selection accuracy: 100%.
- Action success: 100%.
- Unauthorized-access prevention: 100%.

The full run records latency, token usage and estimated model cost in the linked evaluation artifacts.

## Suggested five-minute demonstration

1. Sign in as Arjun Mehta and ask a cited leave-policy question.
2. Ask the hybrid query about policy, current balance, seven days of earned leave and approver.
3. Propose leave, show that no database action occurs before confirmation, then confirm and show the request ID.
4. Sign in as Priya Sharma, review team availability and approve an assigned request.
5. Demonstrate an employee authorization denial, then sign in as Neha Kapoor and generate an organization workforce summary.
6. Show a trace and the committed evaluation report.

## Assumptions and limitations

- Identity selection is intentionally a demo login; production would use SSO/OIDC and HRIS-managed roles.
- PostgreSQL is the synthetic system of record and vector store; external enterprise integrations are out of scope.
- The app supports OpenAI or xAI through configuration. Evaluation requires the selected provider key, while tests do not.
- The rule-based injection detector is defense in depth; authorization is enforced independently in code and database views.
- Single-region and single-tenant deployment is assumed. Production improvements are detailed in the design note.
