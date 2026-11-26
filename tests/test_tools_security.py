"""Authorization, scoped data access, actions and idempotency against the real (seeded) database."""

from __future__ import annotations

import threading
from datetime import date

import pytest

from app.config import get_settings
from app.core.observability import Trace
from app.db import tx
from app.tools import ToolContext, execute
from app.tools.actions import cancel_action, confirm_action
from tests.conftest import make_principal


def ctx(name):
    p = make_principal(name)
    return ToolContext(principal=p, today=get_settings().today, trace=Trace(str(p.session_id), p.user_id, p.role.value))


def pending_for(approver_id: int, employee_id: int | None = None) -> dict:
    with tx() as conn:
        return conn.execute("SELECT * FROM hr.leave_requests WHERE approver_id=%s AND status='pending' "
                            "AND (%s::int IS NULL OR employee_id=%s) ORDER BY start_date LIMIT 1",
                            (approver_id, employee_id, employee_id)).fetchone()


# ------------------------------------------------------------------ scoping
def test_employee_cannot_read_other_employee_balance():
    r = execute(ctx("arjun"), "get_leave_balance", {"employee": "Kavya Nair"})
    assert not r.ok and r.error.startswith("PERMISSION_DENIED")


def test_manager_scope_includes_indirect_reports_but_not_other_teams():
    c = ctx("priya")
    assert execute(c, "get_leave_balance", {"employee": "Arjun Mehta"}).ok              # direct
    indirect = execute(c, "get_team", {"include_indirect": True}).data
    assert any(m["depth"] == 2 for m in indirect["members"])
    sana_report = next(m for m in indirect["members"] if m["depth"] == 2)["name"]
    assert execute(c, "get_leave_balance", {"employee": sana_report}).ok                # indirect
    r = execute(c, "get_leave_balance", {"employee": "Kavya Nair"})                     # other team
    assert not r.ok and "PERMISSION_DENIED" in r.error


def test_role_filtered_tools():
    for tool in ("team_availability", "workforce_stats", "propose_decision", "run_sql_analytics", "detect_anomalies"):
        r = execute(ctx("arjun"), tool, {})
        assert not r.ok and "not available to the employee role" in r.error


def test_hr_sees_org_manager_sees_tree():
    hr = execute(ctx("neha"), "workforce_stats", {"metric": "headcount"}).data["count"]
    mgr = execute(ctx("priya"), "workforce_stats", {"metric": "headcount"}).data["count"]
    with tx() as conn:
        total = conn.execute("SELECT count(*) n FROM hr.employees WHERE status='active'").fetchone()["n"]
    assert hr == total and mgr < hr


def test_every_active_hr_admin_gets_organization_wide_department_headcount():
    with tx() as conn:
        hr_ids = [r["employee_id"] for r in conn.execute(
            "SELECT employee_id FROM hr.app_users WHERE role='hr_admin' AND is_active ORDER BY employee_id"
        ).fetchall()]
        active_total = conn.execute("SELECT count(*) AS n FROM hr.employees WHERE status='active'").fetchone()["n"]
    assert hr_ids, "Seed data must include at least one active HR administrator"
    results = []
    for employee_id in hr_ids:
        response = execute(ctx(employee_id), "workforce_stats", {"metric": "headcount_by_department"})
        assert response.ok, f"HR employee {employee_id} was denied organization analytics: {response.error}"
        assert response.data["scope"] == "organisation"
        assert sum(row["active_employees"] for row in response.data["rows"]) == active_total
        results.append(response.data["rows"])
    assert all(rows == results[0] for rows in results), "All HR admins must receive the same organization-wide totals"


def test_every_active_hr_admin_gets_complete_directory_summary_and_approval_history():
    with tx() as conn:
        hr_ids = [r["employee_id"] for r in conn.execute(
            "SELECT employee_id FROM hr.app_users WHERE role='hr_admin' AND is_active ORDER BY employee_id"
        ).fetchall()]
        employee_total = conn.execute("SELECT count(*) AS n FROM hr.employees").fetchone()["n"]
        approval_total = conn.execute("SELECT count(*) AS n FROM hr.approval_history").fetchone()["n"]
    assert hr_ids
    for employee_id in hr_ids:
        directory = execute(ctx(employee_id), "get_all_employees", {})
        assert directory.ok, f"HR employee {employee_id} cannot access the employee directory: {directory.error}"
        assert directory.data["total_matching"] == employee_total
        assert directory.data["returned"] == employee_total

        summary = execute(ctx(employee_id), "generate_workforce_summary", {"lookback_days": 30})
        assert summary.ok, f"HR employee {employee_id} cannot generate a workforce summary: {summary.error}"
        assert summary.data["headline"]["total_employees"] == employee_total
        assert sum(r["total_employees"] for r in summary.data["departments"]) == employee_total
        assert summary.data["attendance"]

        history = execute(ctx(employee_id), "get_approval_history", {"limit": 1000})
        assert history.ok, f"HR employee {employee_id} cannot access approval history: {history.error}"
        assert history.data["total_matching"] == approval_total
        events = list(history.data["events"])
        while history.data["has_more"]:
            history = execute(ctx(employee_id), "get_approval_history",
                              {"limit": 1000, "offset": history.data["next_offset"]})
            assert history.ok
            events.extend(history.data["events"])
        assert len(events) == approval_total
        assert len({event["history_id"] for event in events}) == approval_total
        assert all(event["request_id"].startswith("LR-") for event in events)


@pytest.mark.parametrize("tool", ["get_all_employees", "generate_workforce_summary", "get_approval_history"])
def test_organization_hr_reporting_tools_are_hr_only(tool):
    for persona in ("arjun", "priya"):
        response = execute(ctx(persona), tool, {})
        assert not response.ok and "not available to the" in response.error


def test_arya_sunder_has_a_consistent_non_hr_employee_profile():
    with tx() as conn:
        arya = conn.execute(
            """SELECT e.designation, d.name AS department, m.full_name AS manager, u.role
                 FROM hr.employees e JOIN hr.departments d ON d.id=e.department_id
                 JOIN hr.employees m ON m.id=e.manager_id JOIN hr.app_users u ON u.employee_id=e.id
                WHERE e.full_name='Arya Sunder'""").fetchone()
    assert arya == {"designation": "Operations Analyst", "department": "Operations",
                    "manager": "Karan Malhotra", "role": "employee"}


def test_list_requests_org_scope_requires_hr():
    r = execute(ctx("priya"), "list_leave_requests", {"scope": "organization"})
    assert not r.ok and "PERMISSION_DENIED" in r.error
    assert execute(ctx("neha"), "list_leave_requests", {"scope": "organization", "status": "pending"}).ok


def test_invalid_arguments_are_rejected_before_execution():
    r = execute(ctx("arjun"), "check_leave_eligibility", {"leave_type": "XX", "start_date": "tomorrow"})
    assert not r.ok and r.error.startswith("Invalid arguments")


def test_employee_can_get_own_monthly_attendance_report():
    r = execute(ctx("arjun"), "attendance_history", {"start_date": "2026-11-01", "end_date": "2026-11-30"})
    assert r.ok
    assert r.data["start_date"] == "2026-11-01" and r.data["end_date"] == "2026-11-25"
    assert r.data["month_to_date"] is True
    assert r.data["summary"]["working_days"] == len(r.data["records"])
    assert all(row["date"] <= "2026-11-25" for row in r.data["records"])


# ------------------------------------------------------------------ eligibility
def test_probation_blocks_earned_leave():
    r = execute(ctx("rohan"), "check_leave_eligibility", {"leave_type": "EL", "start_date": "2026-12-14", "end_date": "2026-12-15"})
    assert r.ok and not r.data["eligible"] and any("probation" in v for v in r.data["violations"])


def test_el_notice_and_skip_level_escalation():
    r = execute(ctx("arjun"), "check_leave_eligibility", {"leave_type": "EL", "start_date": "2026-12-03", "end_date": "2026-12-11"})
    d = r.data
    assert d["eligible"] and d["working_days_requested"] == 7
    # skip-level (IT head) is on approved leave today, so it escalates to the CEO
    assert d["approver_name"] == "Rajesh Iyer" and "escalates" in d["approval_rule"]


def test_cl_max_consecutive():
    r = execute(ctx("arjun"), "check_leave_eligibility", {"leave_type": "CL", "start_date": "2026-12-07", "end_date": "2026-12-10"})
    assert not r.data["eligible"] and any("3 consecutive" in v for v in r.data["violations"])


# ------------------------------------------------------------------ actions
def test_manager_cannot_approve_own_or_unassigned_requests():
    c = ctx("priya")
    own = pending_for(approver_id=2, employee_id=8)  # Priya's own request, assigned to her manager
    r = execute(c, "propose_decision", {"request_id": own["id"], "decision": "approve"})
    assert not r.ok and "own leave request" in r.error
    other = pending_for(approver_id=9)                # assigned to Vikram
    r = execute(c, "propose_decision", {"request_id": other["id"], "decision": "approve"})
    assert not r.ok and "not assigned to you" in r.error


def test_submit_leave_idempotent_and_other_user_cannot_confirm():
    c = ctx("arjun")
    with tx() as conn:
        pending_before = conn.execute("SELECT pending FROM hr.leave_balances WHERE employee_id=20 AND leave_type='CL' AND year=2026").fetchone()["pending"]
    r = execute(c, "propose_leave", {"leave_type": "CL", "start_date": "2026-12-03", "end_date": "2026-12-04"})
    assert r.ok and r.data["status"] == "awaiting_user_confirmation"
    aid = r.data["action_id"]
    # re-proposing the identical leave reuses the same proposal
    assert execute(c, "propose_leave", {"leave_type": "CL", "start_date": "2026-12-03", "end_date": "2026-12-04"}).data["action_id"] == aid
    # another user cannot execute it
    assert confirm_action(make_principal("priya"), aid).status == "failed"
    first = confirm_action(c.principal, aid)
    second = confirm_action(c.principal, aid)
    assert first.status == "executed" and first.request_id and first.request_status == "pending"
    assert second.status == "already_executed" and second.request_id == first.request_id
    with tx() as conn:
        n = conn.execute("SELECT count(*) n FROM hr.leave_requests WHERE employee_id=20 AND start_date='2026-12-03'").fetchone()["n"]
        bal = conn.execute("SELECT pending FROM hr.leave_balances WHERE employee_id=20 AND leave_type='CL' AND year=2026").fetchone()
    assert n == 1 and bal["pending"] == pending_before + 2   # reserved exactly once
    # an overlapping proposal is now refused by the rules engine
    r = execute(c, "propose_leave", {"leave_type": "CL", "start_date": "2026-12-04", "end_date": "2026-12-04"})
    assert r.data["status"] == "not_eligible" and any("Overlaps" in v for v in r.data["violations"])


def test_concurrent_confirms_create_one_request():
    c = ctx("kavya")
    aid = execute(c, "propose_leave", {"leave_type": "SL", "start_date": "2026-12-17", "end_date": "2026-12-17"}).data["action_id"]
    results = []
    threads = [threading.Thread(target=lambda: results.append(confirm_action(c.principal, aid))) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(r.status for r in results).count("executed") == 1
    assert len({r.request_id for r in results}) == 1


def test_approve_flow_updates_status_and_balance():
    c = ctx("priya")
    req = pending_for(approver_id=8, employee_id=27)
    with tx() as conn:
        before = conn.execute("SELECT used, pending FROM hr.leave_balances WHERE employee_id=27 AND leave_type=%s AND year=2026",
                              (req["leave_type"],)).fetchone()
    aid = execute(c, "propose_decision", {"request_id": req["id"], "decision": "approve", "comment": "ok"}).data["action_id"]
    out = confirm_action(c.principal, aid)
    assert out.status == "executed" and out.request_status == "approved"
    with tx() as conn:
        after = conn.execute("SELECT used, pending FROM hr.leave_balances WHERE employee_id=27 AND leave_type=%s AND year=2026",
                             (req["leave_type"],)).fetchone()
        hist = conn.execute("SELECT action FROM hr.approval_history WHERE request_id=%s ORDER BY id", (req["id"],)).fetchall()
    assert after["used"] == before["used"] + req["days"] and after["pending"] == before["pending"] - req["days"]
    assert [h["action"] for h in hist][-1] == "approved"
    # a second decision on the same request is impossible
    r = execute(c, "propose_decision", {"request_id": req["id"], "decision": "reject"})
    assert not r.ok and "already approved" in r.error


def test_cancel_action():
    c = ctx("arjun")
    aid = execute(c, "propose_leave", {"leave_type": "SL", "start_date": "2026-11-24", "end_date": "2026-11-24"}).data["action_id"]
    assert cancel_action(c.principal, aid).status == "cancelled"
    assert confirm_action(c.principal, aid).status == "cancelled"


# ------------------------------------------------------------------ analytics & RAG
def test_assess_pending_requests_three_next_week():
    d = execute(ctx("priya"), "assess_pending_requests", {"start_date": "2026-11-30", "end_date": "2026-12-04"}).data
    assert d["pending_assigned_to_you"] >= 2
    assert all("suggested_review_order" in r for r in d["requests"])


def test_anomalies_find_seeded_issues():
    d = execute(ctx("neha"), "detect_anomalies", {}).data
    assert d["unusual_absence"], "seeded high-absence employee should be flagged"
    assert d["leave_concentration_weeks"], "seeded Operations leave concentration should be flagged"
    inc = d["inconsistencies"]
    assert inc["on_leave_without_approved_request"] and inc["present_during_approved_leave"]


def test_forecast_separates_fact_and_prediction():
    d = execute(ctx("neha"), "forecast_availability", {"start_date": "2026-11-30", "end_date": "2026-12-04", "department": "IT"}).data
    assert "PREDICTION" in d["labels"]["predicted_unplanned_absence"] and d["evaluation"]["mae_people_per_day_baseline"] is not None


@pytest.mark.parametrize("q,doc,section_hint", [
    ("How many casual leaves am I entitled to?", "leave_policy", "2.1"),
    ("Can I carry forward earned leave?", "leave_policy", "4.4"),
    ("What is the work-from-home policy?", "wfh_policy", ""),
    ("Who approves leave longer than five days?", "approval_policy", "1.2"),
    ("What if my manager is unavailable?", "approval_policy", "2."),
])
def test_retrieval_top1(q, doc, section_hint):
    r = execute(ctx("arjun"), "search_policy", {"query": q}).data["results"]
    assert r[0]["document"] == doc and section_hint in r[0]["section"] and r[0]["version"] != "1.0"


def test_retrieval_abstains_and_versions():
    c = ctx("arjun")
    assert execute(c, "search_policy", {"query": "What is the stock option vesting schedule?"}).data["results"] == []
    old = execute(c, "search_policy", {"query": "carry forward earned leave", "as_of": "2025-06-01"}).data["results"]
    assert old[0]["version"] == "1.0" and "10 days" in old[0]["text"]


def test_injected_document_is_neutralised():
    with tx() as conn:
        row = conn.execute("""SELECT c.content, c.flagged_injection FROM kb.chunks c JOIN kb.document_versions v ON v.id=c.version_id
                              JOIN kb.documents d ON d.id=v.document_id WHERE d.doc_key='it_assets_faq' AND c.flagged_injection""").fetchone()
    assert row and "ignore all previous instructions" not in row["content"].lower()


def test_scoped_view_enforced_by_database_for_readonly_role():
    """Even raw SQL as the analytics role only sees the scoped rows."""
    p = make_principal("arjun")
    with tx(p) as conn:
        conn.execute("SET LOCAL ROLE pl_readonly")
        n = conn.execute("SELECT count(*) n FROM hr.v_employees_scoped").fetchone()["n"]
        with pytest.raises(Exception):
            conn.execute("SELECT * FROM hr.employees LIMIT 1")
    assert n == 1
