"""Generate deterministic synthetic workforce data.

Usage: python -m scripts.seed_data   (recreates every schema from app/db/schema.sql)

Results are reproducible: fixed random seed, with "today" taken from APP_TODAY.
Personas with known data are created first, so their IDs are stable:
  1  CEO                          8  Priya Sharma   manager (IT)      persona: manager
  20 Arjun Mehta  employee (IT)   21 Rohan Verma    probation (IT)    22 Sana Iqbal team lead (IT, has reports)
  9  Vikram Singh manager (Sales) 30 Kavya Nair     employee (Sales)  14 Neha Kapoor HR admin
"""

from __future__ import annotations

import random
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from faker import Faker

from app.config import ROOT, get_settings
from app.core.dates import week_start, working_days

SEED = 42
rng = random.Random(SEED)
fake = Faker("en_IN")
Faker.seed(SEED)

DEPARTMENTS = [  # id, name, parent
    (1, "Executive", None),
    (2, "IT", 1),
    (3, "Finance", 1),
    (4, "Human Resources", 1),
    (5, "Sales", 1),
    (6, "Operations", 1),
    (7, "IT Infrastructure", 2),
    (8, "Inside Sales", 5),
]
DESIGNATIONS = {
    2: ["Software Engineer", "Senior Software Engineer", "QA Engineer", "Business Analyst"],
    3: ["Accountant", "Financial Analyst", "Payroll Specialist"],
    4: ["HR Generalist", "Recruiter", "HR Analyst"],
    5: ["Account Executive", "Sales Associate", "Key Account Manager"],
    6: ["Operations Analyst", "Logistics Coordinator", "Process Associate"],
    7: ["Systems Engineer", "Network Engineer"],
    8: ["Inside Sales Representative"],
}
LOCATIONS = ["Bengaluru", "Pune", "Gurugram", "Hyderabad"]


def holidays_from_policy() -> list[tuple[date, str]]:
    text = (ROOT / "data/policies/leave_policy_v2.md").read_text()
    return [(date.fromisoformat(d), n.strip()) for d, n in re.findall(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]+)\|", text)]


def main() -> None:
    s = get_settings()
    today = s.today
    year = today.year
    hol = {d for d, _ in holidays_from_policy()}
    next_mon = week_start(today) + timedelta(days=7)

    with psycopg.connect(s.database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS hr, kb, agent, ops CASCADE")
        conn.execute((ROOT / "app/db/schema.sql").read_text())

    with psycopg.connect(s.database_url) as conn, conn.transaction():
        cur = conn.cursor()
        cur.executemany("INSERT INTO hr.holidays (date, name) VALUES (%s, %s)", holidays_from_policy())
        cur.executemany(
            "INSERT INTO hr.leave_types VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [
                ("CL", "Casual Leave", 12, 0, None, 1, "leave_policy"),
                ("SL", "Sick Leave", 10, 5, 2, 0, "leave_policy"),
                ("EL", "Earned Leave", 18, 15, None, 7, "leave_policy"),
                ("LOP", "Loss of Pay", 0, 0, None, 0, "leave_policy"),
            ],
        )
        cur.executemany("INSERT INTO hr.departments (id, name, parent_id) VALUES (%s,%s,%s)", DEPARTMENTS)

        # ---------------------------------------------------------- employees
        emps: list[dict] = []

        def add(name, dept, desig, mgr, joined, status="active", email=None):
            eid = len(emps) + 1
            slug = re.sub(r"[^a-z]", ".", name.lower()).strip(".")
            emps.append(dict(id=eid, emp_code=f"EMP{eid:04d}", full_name=name, email=email or f"{slug}.{eid}@peoplelens.example",
                             department_id=dept, designation=desig, manager_id=mgr, location=rng.choice(LOCATIONS),
                             joining_date=joined, status=status))
            return eid

        def old_join():
            return today - timedelta(days=rng.randint(400, 3000))

        ceo = add("Rajesh Iyer", 1, "Chief Executive Officer", None, today - timedelta(days=4000))
        heads = {d: add(n, d, "Head of " + nm, ceo, old_join())
                 for d, n, nm in [(2, "Anil Kumar", "IT"), (3, "Meera Pillai", "Finance"), (4, "Deepa Rao", "Human Resources"),
                                  (5, "Suresh Menon", "Sales"), (6, "Farhan Qureshi", "Operations"), (7, "Lakshmi Narayan", "IT Infrastructure")]}
        # managers: ids 8..19 (12 managers)
        mgr_specs = [
            ("Priya Sharma", 2, heads[2]), ("Vikram Singh", 5, heads[5]), ("Rahul Das", 2, heads[2]),
            ("Ananya Ghosh", 3, heads[3]), ("Karan Malhotra", 6, heads[6]), ("Neha Kapoor", 4, heads[4]),
            ("Imran Shaikh", 6, heads[6]), ("Pooja Reddy", 7, heads[7]), ("Arvind Joshi", 8, heads[5]),
            ("Divya Menon", 3, heads[3]), ("Sameer Khan", 5, heads[5]), ("Ritu Agarwal", 7, heads[7]),
        ]
        managers = {}
        for name, dept, boss in mgr_specs:
            title = "HR Business Partner" if dept == 4 else "Engineering Manager" if dept in (2, 7) else "Manager"
            managers[name] = add(name, dept, title, boss, old_join())
        priya, vikram, neha = managers["Priya Sharma"], managers["Vikram Singh"], managers["Neha Kapoor"]
        assert (priya, vikram, neha) == (8, 9, 13), (priya, vikram, neha)

        arjun = add("Arjun Mehta", 2, "Senior Software Engineer", priya, today - timedelta(days=900))      # 20
        rohan = add("Rohan Verma", 2, "Software Engineer", priya, today - timedelta(days=30))              # 21 (probation)
        sana = add("Sana Iqbal", 2, "Tech Lead", priya, old_join())                                       # 22 (team lead)
        for _ in range(3):  # Sana's reports = Priya's indirect reports (23..25)
            add(fake.name(), 2, rng.choice(DESIGNATIONS[2]), sana, old_join())
        for _ in range(4):  # more of Priya's direct team (26..29)
            add(fake.name(), 2, rng.choice(DESIGNATIONS[2]), priya, old_join())
        kavya = add("Kavya Nair", 5, "Account Executive", vikram, old_join())                             # 30
        assert (arjun, rohan, sana, kavya) == (20, 21, 22, 30)

        mgr_dept = {mid: next(d for n, d, _ in mgr_specs if managers[n] == mid) for mid in managers.values()}
        while len(emps) < 220:
            mid = rng.choice(list(managers.values()))
            dept = mgr_dept[mid]
            recent = rng.random() < 0.08
            joined = today - timedelta(days=rng.randint(5, 88)) if recent else old_join()
            status = "inactive" if rng.random() < 0.03 else ("on_notice" if rng.random() < 0.02 else "active")
            add(fake.name(), dept, rng.choice(DESIGNATIONS[dept]), mid, joined, status)

        # Keep Arya as a regular employee persona without a misleading HR designation.
        # The generated identity is deterministic (EMP0082), so normalise the whole reporting profile together.
        arya = next(e for e in emps if e["full_name"] == "Arya Sunder")
        arya.update(department_id=6, designation="Operations Analyst", manager_id=managers["Karan Malhotra"])

        # Priya's skip-level (Anil, IT head) is on leave today, so escalation can be tested via >5-day requests
        cur.executemany(
            """INSERT INTO hr.employees (id, emp_code, full_name, email, department_id, designation, manager_id,
                   location, joining_date, status) VALUES (%(id)s,%(emp_code)s,%(full_name)s,%(email)s,%(department_id)s,
                   %(designation)s,%(manager_id)s,%(location)s,%(joining_date)s,%(status)s)""",
            sorted(emps, key=lambda e: (e["manager_id"] is not None, e["manager_id"] or 0, e["id"])),
        )
        conn.execute("SELECT setval('hr.employees_id_seq', (SELECT max(id) FROM hr.employees))")
        for d, hid in heads.items():
            conn.execute("UPDATE hr.departments SET head_employee_id=%s WHERE id=%s", (hid, d))
        conn.execute("UPDATE hr.departments SET head_employee_id=%s WHERE id=1", (ceo,))

        # ---------------------------------------------------------- positions
        pos = []
        for e in emps:
            pos.append((e["designation"], e["department_id"], rng.choice(["L1", "L2", "L3", "L4"]),
                        "filled" if e["status"] != "inactive" else "vacant", e["id"] if e["status"] != "inactive" else None))
        for _ in range(15):
            d = rng.choice([2, 3, 5, 6, 7])
            pos.append((rng.choice(DESIGNATIONS[d]), d, "L2", "vacant", None))
        for _ in range(3):
            d = rng.choice([3, 6])
            pos.append((rng.choice(DESIGNATIONS[d]), d, "L3", "frozen", None))
        for p in pos:
            r = conn.execute("INSERT INTO hr.positions (title, department_id, grade, assignment_status, employee_id) "
                             "VALUES (%s,%s,%s,%s,%s) RETURNING id", p).fetchone()
            if p[4]:
                conn.execute("UPDATE hr.employees SET position_id=%s WHERE id=%s", (r[0], p[4]))

        # ---------------------------------------------------------- roles
        has_reports = {e["manager_id"] for e in emps if e["manager_id"]}
        for e in emps:
            role = "hr_admin" if e["id"] in (neha, heads[4]) else "manager" if e["id"] in has_reports else "employee"
            conn.execute("INSERT INTO hr.app_users (employee_id, role, is_active) VALUES (%s,%s,%s)",
                         (e["id"], role, e["status"] != "inactive"))

        # ---------------------------------------------------------- leave requests
        by_id = {e["id"]: e for e in emps}

        def approver_for(eid: int, days: int) -> int:
            m = by_id[eid]["manager_id"]
            if days > 5 and by_id[m]["manager_id"]:
                return by_id[m]["manager_id"]
            return m

        booked: dict[int, list[tuple[date, date]]] = {e["id"]: [] for e in emps}
        requests: list[dict] = []

        def free(eid, s_, e_):
            return all(e_ < a or s_ > b for a, b in booked[eid])

        def req(eid, lt, s_, e_, status, created=None, decided=None):
            n = len(working_days(s_, e_, hol))
            if n == 0 or not free(eid, s_, e_) or by_id[eid]["manager_id"] is None:
                return False
            booked[eid].append((s_, e_))
            created = created or (s_ - timedelta(days=rng.randint(8, 20)))
            requests.append(dict(employee_id=eid, leave_type=lt, start_date=s_, end_date=e_, days=Decimal(n), status=status,
                                 approver_id=approver_for(eid, n), created_at=created,
                                 decided_at=decided if decided else (created + timedelta(days=rng.randint(0, 2)) if status in ("approved", "rejected") else None)))
            return True

        # --- scripted scenarios
        anil = heads[2]
        req(anil, "EL", today - timedelta(days=2), today + timedelta(days=3), "approved")                # IT head away today
        req(arjun, "CL", date(year, 3, 10), date(year, 3, 11), "approved")
        req(arjun, "EL", date(year, 6, 8), date(year, 6, 12), "approved")
        req(arjun, "SL", date(year, 9, 2), date(year, 9, 2), "approved")
        req(arjun, "CL", next_mon + timedelta(days=14), next_mon + timedelta(days=14), "pending", created=today - timedelta(days=1))
        # 6.5 scenario: exactly three pending requests from Priya's direct team for next week
        req(26, "EL", next_mon, next_mon + timedelta(days=2), "pending", created=today - timedelta(days=9))
        req(27, "CL", next_mon + timedelta(days=1), next_mon + timedelta(days=2), "pending", created=today - timedelta(days=1))
        req(28, "EL", next_mon + timedelta(days=2), next_mon + timedelta(days=4), "pending", created=today - timedelta(days=3))
        req(29, "CL", next_mon + timedelta(days=1), next_mon + timedelta(days=1), "approved", created=today - timedelta(days=6))
        # Sana's report already has approved leave next week (indirect report on leave)
        req(23, "EL", next_mon + timedelta(days=3), next_mon + timedelta(days=4), "approved", created=today - timedelta(days=12))
        # Kavya (outside Priya's team) pending, assigned to Vikram
        req(kavya, "CL", next_mon + timedelta(days=7), next_mon + timedelta(days=8), "pending", created=today - timedelta(days=2))
        # Priya's own request (assigned to her manager): she must not be able to approve it
        req(priya, "CL", next_mon + timedelta(days=10), next_mon + timedelta(days=10), "pending", created=today - timedelta(days=1))

        # --- random history (Jan 1 to ~3 weeks ahead)
        scripted = {arjun, 26, 27, 28, 29, 23, priya, kavya, anil}
        horizon_start, horizon_end = date(year, 1, 5), today + timedelta(days=21)
        for e in emps:
            if e["id"] in scripted or e["manager_id"] is None:
                continue
            for _ in range(rng.randint(2, 5)):
                lt = rng.choices(["CL", "SL", "EL"], weights=[4, 3, 3])[0]
                start = horizon_start + timedelta(days=rng.randint(0, (horizon_end - horizon_start).days))
                if start < e["joining_date"]:
                    continue
                length = {"CL": rng.randint(1, 2), "SL": rng.randint(1, 3), "EL": rng.randint(2, 7)}[lt]
                end = start + timedelta(days=length - 1)
                if start > today:
                    status = rng.choices(["pending", "approved"], weights=[2, 1])[0]
                    created = today - timedelta(days=rng.randint(0, 10))
                else:
                    status = rng.choices(["approved", "rejected", "cancelled"], weights=[85, 10, 5])[0]
                    created = None
                req(e["id"], lt, start, end, status, created=created)

        # Operations leave concentration: many leaves in one week (anomaly target)
        ops_team = [e["id"] for e in emps if e["manager_id"] == managers["Karan Malhotra"] and e["status"] == "active"]
        conc_start = week_start(today - timedelta(days=35))
        for eid in ops_team[:7]:
            req(eid, "EL", conc_start + timedelta(days=1), conc_start + timedelta(days=3), "approved")

        # ---------------------------------------------------------- balances (consistent with requests)
        used: dict[tuple[int, str], Decimal] = {}
        pend: dict[tuple[int, str], Decimal] = {}
        for r in requests:
            if r["start_date"].year != year:
                continue
            k = (r["employee_id"], r["leave_type"])
            if r["status"] == "approved":
                used[k] = used.get(k, 0) + r["days"]
            elif r["status"] == "pending":
                pend[k] = pend.get(k, 0) + r["days"]
        months = today.month
        for e in emps:
            for lt, acc in (("CL", Decimal(12)), ("SL", Decimal(10)),
                            ("EL", Decimal("1.5") * months + Decimal(rng.randint(0, 15)))):
                if e["id"] == arjun and lt == "EL":
                    acc = Decimal("1.5") * months + Decimal(4)
                if e["joining_date"].year == year:
                    acc = (acc * Decimal((12 - e["joining_date"].month + 1) / 12)).quantize(Decimal("0.5"))
                u, p = used.get((e["id"], lt), Decimal(0)), pend.get((e["id"], lt), Decimal(0))
                acc = max(acc, u + p)  # never negative
                conn.execute("INSERT INTO hr.leave_balances (employee_id, leave_type, year, accrued, used, pending) "
                             "VALUES (%s,%s,%s,%s,%s,%s)", (e["id"], lt, year, acc, u, p))

        for r in sorted(requests, key=lambda r: r["created_at"]):
            rid = conn.execute(
                """INSERT INTO hr.leave_requests (employee_id, leave_type, start_date, end_date, days, status, approver_id,
                       created_via, created_at, decided_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'seed',%s,%s) RETURNING id""",
                (r["employee_id"], r["leave_type"], r["start_date"], r["end_date"], r["days"], r["status"],
                 r["approver_id"], r["created_at"], r["decided_at"]),
            ).fetchone()[0]
            conn.execute("INSERT INTO hr.approval_history (request_id, actor_id, action, acted_at) VALUES (%s,%s,'submitted',%s)",
                         (rid, r["employee_id"], r["created_at"]))
            if r["status"] in ("approved", "rejected"):
                conn.execute("INSERT INTO hr.approval_history (request_id, actor_id, action, acted_at) VALUES (%s,%s,%s,%s)",
                             (rid, r["approver_id"], r["status"], r["decided_at"]))
            elif r["status"] == "cancelled":
                conn.execute("INSERT INTO hr.approval_history (request_id, actor_id, action, acted_at) VALUES (%s,%s,'cancelled',%s)",
                             (rid, r["employee_id"], r["created_at"] + timedelta(days=1)))

        # ---------------------------------------------------------- notifications (open approvals + recent decisions)
        rng_fmt = """CASE WHEN r.start_date = r.end_date THEN to_char(r.start_date, 'DD Mon')
                          ELSE to_char(r.start_date, 'DD Mon') || ' – ' || to_char(r.end_date, 'DD Mon') END"""
        conn.execute(f"""
            INSERT INTO hr.notifications (recipient_id, kind, title, body, request_id, created_at)
            SELECT r.approver_id, 'approval_needed', 'Leave request from ' || e.full_name,
                   r.leave_type || ' · ' || {rng_fmt} || ' · ' || regexp_replace(r.days::text, '\\.0$', '') || CASE WHEN r.days = 1 THEN ' day' ELSE ' days' END,
                   r.id, r.created_at
              FROM hr.leave_requests r JOIN hr.employees e ON e.id = r.employee_id WHERE r.status = 'pending'""")
        conn.execute(f"""
            INSERT INTO hr.notifications (recipient_id, kind, title, body, request_id, created_at, is_read)
            SELECT r.employee_id, 'leave_' || r.status, 'Leave ' || r.status || ': ' || r.id,
                   r.leave_type || ' · ' || {rng_fmt} || ' · by ' || ap.full_name, r.id, r.decided_at, r.decided_at < %s::date - 3
              FROM hr.leave_requests r JOIN hr.employees ap ON ap.id = r.approver_id
             WHERE r.status IN ('approved', 'rejected') AND r.decided_at >= %s::date - 14""", (today, today))

        # ---------------------------------------------------------- attendance (last ~3 months)
        approved = {}
        for r in requests:
            if r["status"] == "approved":
                d = r["start_date"]
                while d <= r["end_date"]:
                    approved[(r["employee_id"], d)] = True
                    d += timedelta(days=1)
        absentee = next(e["id"] for e in emps if e["department_id"] == 5 and e["id"] > 40 and e["status"] == "active")
        rows = []
        start = today - timedelta(days=92)
        d = start
        while d < today:
            for e in emps:
                if e["joining_date"] > d or e["status"] == "inactive":
                    continue
                if d.weekday() >= 5:
                    continue
                if d in hol:
                    st = "holiday"
                elif approved.get((e["id"], d)):
                    st = "on_leave"
                else:
                    st = rng.choices(["present", "wfh", "absent"], weights=[84, 14, 2])[0]
                    if e["id"] == absentee and d >= today - timedelta(days=30) and rng.random() < 0.45:
                        st = "absent"
                ci = f"{rng.randint(9, 10)}:{rng.randint(0, 59):02d}" if st in ("present", "wfh") else None
                co = f"{rng.randint(18, 19)}:{rng.randint(0, 59):02d}" if ci else None
                rows.append((e["id"], d, st, ci, co))
            d += timedelta(days=1)
        # inconsistencies: on_leave without approved leave; present during approved leave
        idx = {(r[0], r[1]): i for i, r in enumerate(rows)}
        for eid, back in ((41, 10), (57, 17), (88, 24)):
            day = today - timedelta(days=back)
            while day.weekday() >= 5 or day in hol:
                day -= timedelta(days=1)
            if (eid, day) in idx and not approved.get((eid, day)):
                i = idx[(eid, day)]
                rows[i] = (eid, day, "on_leave", None, None)
        flipped = 0
        for (eid, day), _ in sorted(approved.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            if flipped >= 2 or eid in scripted:
                continue
            if (eid, day) in idx and today - timedelta(days=50) < day < today - timedelta(days=5):
                i = idx[(eid, day)]
                if rows[i][2] == "on_leave":
                    rows[i] = (eid, day, "present", "09:40", "18:30")
                    flipped += 1
        with cur.copy("COPY hr.attendance (employee_id, date, status, check_in, check_out) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)

        n_req = conn.execute("SELECT count(*) FROM hr.leave_requests").fetchone()[0]
        n_att = conn.execute("SELECT count(*) FROM hr.attendance").fetchone()[0]
        print(f"seeded: {len(emps)} employees, {len(managers)} managers, {n_req} leave requests, {n_att} attendance rows, "
              f"today={today}, next_week={next_mon}")


if __name__ == "__main__":
    main()
