"""People Lens web app: assistant, leave dashboard, approvals and HR view.

Run: streamlit run ui/streamlit_app.py   (the API must be running; see scripts/run.sh)
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone

import httpx
import pandas as pd
import streamlit as st

API = os.getenv("API_URL", "http://localhost:8000")
TYPE_NAMES = {"CL": "Casual", "SL": "Sick", "EL": "Earned", "LOP": "Loss of pay"}

st.set_page_config(page_title="People Lens", page_icon="◐", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
  #MainMenu, footer, header [data-testid="stToolbar"], [data-testid="stDecoration"], .stDeployButton {display: none !important;}
  header[data-testid="stHeader"] {background: transparent; height: 0;}
  .block-container {padding-top: 2rem; max-width: 1180px;}
  [data-testid="stSidebar"] {min-width: 250px; max-width: 250px;}
  [data-testid="stMetricValue"] {font-size: 1.9rem;}
  .muted {color: rgba(120,120,120,.95); font-size: .85rem;}
  .note {padding: .55rem 0; border-bottom: 1px solid rgba(128,128,128,.18);}
  .note b {font-weight: 600;}
  .dot {display:inline-block; width:.45rem; height:.45rem; border-radius:50%; background:#2f6fed; margin-right:.4rem;}
  .pill {display:inline-block; padding:.05rem .5rem; border-radius:999px; font-size:.78rem; border:1px solid rgba(128,128,128,.35);}
</style>""", unsafe_allow_html=True)

ss = st.session_state


# ------------------------------------------------------------------ API helpers
def api(method: str, path: str, quiet: bool = False, **kw):
    headers = {"Authorization": f"Bearer {ss.token}"} if ss.get("token") else {}
    try:
        r = httpx.request(method, f"{API}{path}", headers=headers, timeout=180, **kw)
    except httpx.HTTPError:
        st.error("The service is unavailable right now. Please try again shortly.")
        st.stop()
    if r.status_code == 401 and ss.get("token"):
        ss.clear()
        st.rerun()
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail")
        except ValueError:
            msg = None
        if quiet:
            return {"_error": msg if isinstance(msg, str) else "Something went wrong."}
        st.error(msg if isinstance(msg, str) else "Something went wrong.")
        st.stop()
    return r.json()


def fmt_range(s: str, e: str) -> str:
    a, b = date.fromisoformat(s), date.fromisoformat(e)
    if a == b:
        return a.strftime("%d %b %Y")
    if a.year == b.year:
        return f"{a.strftime('%d %b')} – {b.strftime('%d %b %Y')}"
    return f"{a.strftime('%d %b %Y')} – {b.strftime('%d %b %Y')}"


def ago(ts: str) -> str:
    t = datetime.fromisoformat(ts)
    secs = (datetime.now(timezone.utc) - t).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return t.strftime("%d %b")


def num(x) -> str:
    return f"{float(x):g}"


# ------------------------------------------------------------------ sign in
def sign_in() -> None:
    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        st.markdown("<div style='height:12vh'></div>", unsafe_allow_html=True)
        st.title("People Lens")
        st.caption("Leave, attendance and HR policies in one place.")
        people = httpx.get(f"{API}/auth/directory", timeout=15).json()
        labels = {f"{p['name']} — {p['designation']}, {p['department']}": p["employee_id"] for p in people}
        default = next((i for i, k in enumerate(labels) if k.startswith("Arjun Mehta")), 0)
        who = st.selectbox("Sign in as", list(labels), index=default)
        if st.button("Continue", type="primary", use_container_width=True):
            r = httpx.post(f"{API}/auth/login", json={"employee_id": labels[who]}, timeout=15)
            if r.status_code != 200:
                st.error("Unable to sign in.")
                return
            ss.token = r.json()["token"]
            ss.me = api("GET", "/me")
            ss.session_id = api("POST", "/sessions")["session_id"]
            ss.chat, ss.page = [], "Assistant"
            st.rerun()


# ------------------------------------------------------------------ sidebar
def attendance_card() -> None:
    a = api("GET", "/attendance/today")
    st.markdown("**Today**")
    if a.get("on_leave"):
        st.caption("You're on leave today.")
    elif not a.get("working_day", True):
        st.caption("No check-in needed today.")
    elif a["status"] == "not_checked_in":
        st.caption("Not checked in yet")
        c1, c2 = st.columns(2)
        if c1.button("Check in", use_container_width=True, type="primary"):
            res = api("POST", "/attendance/check-in", json={"mode": "office"}, quiet=True)
            st.toast(res.get("_error") or f"Checked in at {res['check_in']}")
            st.rerun()
        if c2.button("WFH", use_container_width=True, help="Check in, working from home"):
            res = api("POST", "/attendance/check-in", json={"mode": "wfh"}, quiet=True)
            st.toast(res.get("_error") or f"Checked in (WFH) at {res['check_in']}")
            st.rerun()
    elif a["check_out"]:
        st.caption(f"In {a['check_in']} · Out {a['check_out']}")
    else:
        where = "from home" if a["status"] == "wfh" else "at office"
        st.caption(f"Checked in {where} at {a['check_in']}" + (" · late" if a.get("late") else ""))
        if st.button("Check out", use_container_width=True):
            res = api("POST", "/attendance/check-out", quiet=True)
            st.toast(res.get("_error") or f"Checked out at {res['check_out']}")
            st.rerun()


def sidebar(notes: dict, n_approvals: int) -> str:
    me = ss.me
    with st.sidebar:
        st.markdown(f"### {me['name']}")
        st.caption(f"{me['designation']} · {me['department']}")
        unread = notes["unread"]
        pages = ["Assistant", f"Dashboard  ·  {unread}" if unread else "Dashboard"]
        if me["role"] in ("manager", "hr_admin") or n_approvals:
            pages.append(f"Approvals{f'  ·  {n_approvals}' if n_approvals else ''}")
        if me["role"] == "hr_admin":
            pages.append("All leaves")
        current = next((p for p in pages if p.split("  ·")[0] == ss.page), pages[0])
        choice = st.radio("Navigate", pages, index=pages.index(current), label_visibility="collapsed")
        ss.page = choice.split("  ·")[0]
        st.divider()
        attendance_card()
        st.divider()
        if st.button("Sign out", use_container_width=True):
            ss.clear()
            st.rerun()
    return ss.page


# ------------------------------------------------------------------ assistant
SUGGESTIONS = {"Check in": "Check me in for today", "Apply for leave": "I want to apply for leave",
               "My balance": "What is my leave balance?", "Leave policy": "Summarise the leave policy for me"}


def render_sources(citations: list[dict]) -> None:
    if not citations:
        return
    parts = []
    for c in citations:
        path = c["section_path"].split(" > ")
        parts.append(f"{c['ref']} {path[0]} — {path[-1]}" if len(path) > 1 else f"{c['ref']} {path[0]}")
    st.markdown(f"<div class='muted'>Sources: {' · '.join(parts)}</div>", unsafe_allow_html=True)


def proposal_card(pa: dict, idx: int) -> None:
    p = pa["payload"]
    with st.container(border=True):
        if pa["type"] == "submit_leave":
            st.markdown(f"**{TYPE_NAMES.get(p['leave_type'], p['leave_type'])} leave** · {fmt_range(p['start_date'], p['end_date'])}"
                        f" · {num(p['days'])} day{'s' if float(p['days']) != 1 else ''}")
            st.caption(f"Approver: {p['approver_name']} · Balance after: {num(p['balance_after'])}")
        else:
            st.markdown(f"**{p['decision'].capitalize()}** {p['request_id']} · {p['requester_name']}")
        c1, c2, _ = st.columns([1, 1, 3])
        if c1.button("Confirm", key=f"ok{idx}", type="primary", use_container_width=True):
            out = api("POST", f"/actions/{pa['action_id']}/confirm", json={"session_id": ss.session_id})
            ss.chat[idx]["pending"] = None
            ss.chat.append({"role": "assistant", "content": out["message"]})
            st.rerun()
        if c2.button("Cancel", key=f"no{idx}", use_container_width=True):
            out = api("POST", f"/actions/{pa['action_id']}/cancel", json={"session_id": ss.session_id})
            ss.chat[idx]["pending"] = None
            ss.chat.append({"role": "assistant", "content": out["message"]})
            st.rerun()


def send(prompt: str) -> None:
    ss.chat.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"), st.spinner(""):
        r = api("POST", "/chat", json={"session_id": ss.session_id, "message": prompt})
    for m in ss.chat:
        m["pending"] = None   # only the newest proposal stays actionable
    ss.chat.append({"role": "assistant", "content": r["answer"], "citations": r.get("citations") or [],
                    "pending": r.get("pending_action")})
    st.rerun()


def page_assistant() -> None:
    first = ss.me["name"].split()[0]
    if not ss.chat:
        st.markdown(f"## Hi {first}")
        st.markdown("<span class='muted'>Check in, apply for leave, or ask anything about HR policies.</span>",
                    unsafe_allow_html=True)
        cols = st.columns(len(SUGGESTIONS) + 2)
        for col, (label, prompt_text) in zip(cols, SUGGESTIONS.items()):
            if col.button(label, use_container_width=True):
                ss.queued = prompt_text
    for i, m in enumerate(ss.chat):
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m["role"] == "assistant":
                render_sources(m.get("citations") or [])
                if m.get("pending"):
                    proposal_card(m["pending"], i)
    prompt = st.chat_input("Message People Lens")
    if ss.get("queued"):
        prompt, ss.queued = ss.queued, None
    if prompt:
        send(prompt)


# ------------------------------------------------------------------ dashboard
def notifications_panel(notes: dict) -> None:
    head, action = st.columns([3, 2])
    head.markdown("#### Notifications")
    if notes["unread"] and action.button("Mark all read", use_container_width=True):
        api("POST", "/notifications/read", json={})
        st.rerun()
    if not notes["items"]:
        st.caption("Nothing new.")
    for n in notes["items"][:8]:
        dot = "<span class='dot'></span>" if not n["is_read"] else ""
        st.markdown(f"<div class='note'>{dot}<b>{n['title']}</b><br><span class='muted'>{n['body'] or ''} · {ago(n['created_at'])}"
                    f"</span></div>", unsafe_allow_html=True)


def requests_frame(rows: list[dict], with_employee: bool = False) -> pd.DataFrame:
    data = []
    for r in rows:
        row = {"Request": r["id"]}
        if with_employee:
            row |= {"Employee": r["employee"], "Department": r["department"]}
        row |= {"Type": TYPE_NAMES.get(r["leave_type"], r["leave_type"]), "Dates": fmt_range(r["start_date"], r["end_date"]),
                "Days": float(r["days"]), "Status": r["status"].capitalize(), "Approver": r["approver"],
                "Applied": datetime.fromisoformat(r["created_at"]).strftime("%d %b %Y")}
        if r.get("decision_note"):
            row["Note"] = r["decision_note"]
        data.append(row)
    df = pd.DataFrame(data)
    if "Note" in df:
        df["Note"] = df["Note"].fillna("")
    return df


def balance_row(balances: list[dict]) -> None:
    cols = st.columns(len(balances) or 1)
    for col, b in zip(cols, balances):
        pending = f" · {num(b['pending'])} pending" if float(b["pending"]) else ""
        col.metric(b["name"], num(b["available"]))
        col.caption(f"of {num(b['accrued'])} · {num(b['used'])} used{pending}")


def page_dashboard(notes: dict) -> None:
    left, right = st.columns([2.2, 1], gap="large")
    with left:
        top, btn = st.columns([3, 1])
        top.markdown("## My leave")
        if btn.button("Apply for leave", type="primary", use_container_width=True):
            ss.page, ss.queued = "Assistant", "I want to apply for leave"
            st.rerun()
        balance_row(api("GET", "/me/balances")["balances"])
        st.markdown("#### Requests")
        rows = api("GET", "/me/leave-requests")
        if rows:
            st.dataframe(requests_frame(rows), hide_index=True, use_container_width=True,
                         column_config={"Days": st.column_config.NumberColumn(format="%g")})
        else:
            st.caption("No leave requests yet.")
    with right:
        notifications_panel(notes)


# ------------------------------------------------------------------ approvals
@st.dialog("Reject leave request")
def reject_dialog(r: dict) -> None:
    st.markdown(f"**{r['employee']}** · {TYPE_NAMES.get(r['leave_type'], r['leave_type'])} leave · "
                f"{fmt_range(r['start_date'], r['end_date'])}")
    with st.form(f"reject_{r['id']}", border=False):
        reason = st.text_input("Reason (shared with the employee)", placeholder="Optional")
        if st.form_submit_button("Reject request", type="primary", use_container_width=True):
            res = api("POST", f"/leave-requests/{r['id']}/decision",
                      json={"decision": "reject", "comment": reason or None}, quiet=True)
            st.toast(res.get("_error") or f"Rejected {r['employee']}'s leave")
            st.rerun()


def page_approvals() -> None:
    rows = api("GET", "/approvals")
    st.markdown("## Approvals")
    if not rows:
        st.caption("You're all caught up — no requests waiting for you.")
        return
    st.caption(f"{len(rows)} request{'s' if len(rows) != 1 else ''} waiting for your decision")
    for r in rows:
        with st.container(border=True):
            info, act = st.columns([3.2, 1.4])
            with info:
                st.markdown(f"**{r['employee']}** <span class='muted'>· {r['designation']}</span>", unsafe_allow_html=True)
                st.markdown(f"{TYPE_NAMES.get(r['leave_type'], r['leave_type'])} leave · {fmt_range(r['start_date'], r['end_date'])}"
                            f" · {num(r['days'])} day{'s' if float(r['days']) != 1 else ''}")
                bits = [f"Balance after: {num(r['balance_after'])}" if r["balance_after"] is not None else None,
                        f"{r['teammates_away']} teammate{'s' if r['teammates_away'] != 1 else ''} away" if r["teammates_away"] else None,
                        f"Applied {datetime.fromisoformat(r['created_at']).strftime('%d %b')}",
                        f"“{r['reason']}”" if r.get("reason") else None]
                st.markdown(f"<span class='muted'>{' · '.join(b for b in bits if b)}</span>", unsafe_allow_html=True)
            with act:
                c1, c2 = st.columns(2)
                if c1.button("Approve", key=f"a{r['id']}", type="primary", use_container_width=True):
                    res = api("POST", f"/leave-requests/{r['id']}/decision", json={"decision": "approve"}, quiet=True)
                    st.toast(res.get("_error") or f"Approved {r['employee']}'s leave")
                    st.rerun()
                if c2.button("Reject", key=f"r{r['id']}", use_container_width=True):
                    reject_dialog(r)


# ------------------------------------------------------------------ HR
def page_all_leaves() -> None:
    st.markdown("## All leaves")
    s = api("GET", "/hr/summary")
    c = st.columns(4)
    c[0].metric("Pending", s["pending"])
    c[1].metric("On leave today", s["on_leave_today"])
    c[2].metric("Approved this month", s["approved_this_month"])
    c[3].metric("Headcount", s["headcount"])

    tab_req, tab_emp = st.tabs(["Requests", "By employee"])
    with tab_req:
        f = st.columns([2, 1.4, 1.1, 1.1])
        q = f[0].text_input("Search", placeholder="Name or request ID", label_visibility="collapsed")
        dept = f[1].selectbox("Department", ["All departments"] + api("GET", "/departments"), label_visibility="collapsed")
        status = f[2].selectbox("Status", ["Any status", "Pending", "Approved", "Rejected", "Cancelled"], label_visibility="collapsed")
        role = f[3].selectbox("Role", ["Everyone", "Managers", "Employees", "HR"], label_visibility="collapsed")
        params = {"q": q or None, "department": None if dept.startswith("All") else dept,
                  "status": None if status.startswith("Any") else status.lower(),
                  "role": {"Managers": "manager", "Employees": "employee", "HR": "hr_admin"}.get(role)}
        rows = api("GET", "/hr/leave-requests", params={**{k: v for k, v in params.items() if v}, "limit": 1000})
        st.caption(f"{len(rows)} request{'s' if len(rows) != 1 else ''}")
        if rows:
            st.dataframe(requests_frame(rows, with_employee=True), hide_index=True, use_container_width=True, height=520,
                         column_config={"Days": st.column_config.NumberColumn(format="%g")})
    with tab_emp:
        people = api("GET", "/employees")
        labels = {f"{p['name']} — {p['designation']}, {p['department']}" + (" (manager)" if p["role"] == "manager" else ""):
                  p["employee_id"] for p in people}
        who = st.selectbox("Employee", list(labels), index=None, placeholder="Choose an employee")
        if who:
            data = api("GET", f"/employees/{labels[who]}/leave")
            balance_row(data["balances"])
            if data["requests"]:
                st.dataframe(requests_frame(data["requests"]), hide_index=True, use_container_width=True,
                             column_config={"Days": st.column_config.NumberColumn(format="%g")})
            else:
                st.caption("No leave requests.")


# ------------------------------------------------------------------ main
if not ss.get("token"):
    sign_in()
    st.stop()

notes = api("GET", "/notifications", params={"limit": 20})
approvals_count = len(api("GET", "/approvals")) if ss.me["role"] in ("manager", "hr_admin") else 0
page = sidebar(notes, approvals_count)
if page == "Assistant":
    page_assistant()
elif page == "Dashboard":
    page_dashboard(notes)
elif page == "Approvals":
    page_approvals()
elif page == "All leaves" and ss.me["role"] == "hr_admin":
    page_all_leaves()
