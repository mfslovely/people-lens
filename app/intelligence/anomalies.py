"""Anomaly detection. Statistics first; the LLM only explains the results.

1. Unusual absence: per-employee unplanned-absence rate over the lookback window, scored with a robust z-score
   (median/MAD), flagged at z >= 3.5 (Iglewicz & Hoaglin) with at least 3 absences.
2. Leave concentration: per team and ISO week, the share of the team on approved leave, flagged when >= 40%.
3. Inconsistent records: attendance 'on_leave' without an approved request, 'present' during approved leave,
   and active employees reporting to an inactive manager.
All queries use scoped views, so results respect the caller's access scope.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np


def run(conn, today: date, lookback_days: int, department: str | None) -> dict:
    since = today - timedelta(days=lookback_days)
    dept = "AND e.department ILIKE %(dept)s" if department else ""
    p = {"since": since, "today": today, "dept": department}

    rows = conn.execute(f"""
        SELECT e.id, e.full_name, e.department,
               count(*) FILTER (WHERE a.status='absent') AS absences,
               count(*) FILTER (WHERE a.status NOT IN ('holiday')) AS workdays
          FROM hr.v_attendance_scoped a JOIN hr.v_employees_scoped e ON e.id=a.employee_id
         WHERE a.date >= %(since)s AND a.date < %(today)s AND e.status='active' {dept}
         GROUP BY 1,2,3 HAVING count(*) > 5""", p).fetchall()
    absence = []
    if len(rows) >= 5:
        rates = np.array([r["absences"] / r["workdays"] for r in rows])
        med = float(np.median(rates))
        mad = float(np.median(np.abs(rates - med))) or 1e-6
        for r, rate in zip(rows, rates):
            z = 0.6745 * (rate - med) / mad
            if z >= 3.5 and r["absences"] >= 3:
                absence.append({"employee": r["full_name"], "department": r["department"], "absences": r["absences"],
                                "working_days_observed": r["workdays"], "absence_rate_pct": round(100 * rate, 1),
                                "robust_z": round(z, 1)})
        baseline = {"median_absence_rate_pct": round(100 * med, 2), "employees_analysed": len(rows)}
    else:
        baseline = {"note": "too few employees in scope for statistical scoring", "employees_analysed": len(rows)}
    absence.sort(key=lambda x: -x["robust_z"])

    conc = conn.execute(f"""
        WITH team AS (SELECT manager_id, count(*) AS size FROM hr.v_employees_scoped e WHERE status='active' {dept} GROUP BY 1),
             wk AS (SELECT e.manager_id, date_trunc('week', d)::date AS week, count(DISTINCT l.employee_id) AS on_leave
                      FROM hr.v_leave_scoped l JOIN hr.v_employees_scoped e ON e.id=l.employee_id
                      CROSS JOIN LATERAL generate_series(l.start_date, l.end_date, interval '1 day') d
                     WHERE l.status='approved' AND l.start_date >= %(since)s AND l.start_date < %(today)s {dept}
                     GROUP BY 1,2)
        SELECT m.full_name AS manager, wk.week, wk.on_leave, team.size,
               round(100.0 * wk.on_leave / team.size) AS pct
          FROM wk JOIN team USING (manager_id) JOIN hr.v_employees_scoped m ON m.id = wk.manager_id
         WHERE team.size >= 4 AND wk.on_leave::numeric / team.size >= 0.4
         ORDER BY pct DESC LIMIT 10""", p).fetchall()

    leave_no_req = conn.execute(f"""
        SELECT e.full_name, a.date FROM hr.v_attendance_scoped a JOIN hr.v_employees_scoped e ON e.id=a.employee_id
         WHERE a.status='on_leave' AND a.date >= %(since)s {dept}
           AND NOT EXISTS (SELECT 1 FROM hr.v_leave_scoped l WHERE l.employee_id=a.employee_id AND l.status='approved'
                           AND a.date BETWEEN l.start_date AND l.end_date)
         ORDER BY a.date""", p).fetchall()
    present_on_leave = conn.execute(f"""
        SELECT e.full_name, a.date, l.id AS request_id FROM hr.v_attendance_scoped a
          JOIN hr.v_employees_scoped e ON e.id=a.employee_id
          JOIN hr.v_leave_scoped l ON l.employee_id=a.employee_id AND l.status='approved' AND a.date BETWEEN l.start_date AND l.end_date
         WHERE a.status IN ('present','wfh') AND a.date >= %(since)s {dept} ORDER BY a.date""", p).fetchall()
    orphan = conn.execute(f"""
        SELECT e.full_name AS employee, m.full_name AS manager, m.status AS manager_status
          FROM hr.v_employees_scoped e JOIN hr.v_employees_scoped m ON m.id=e.manager_id
         WHERE e.status='active' AND m.status='inactive' {dept}""", p).fetchall()

    return {
        "window": {"from": since, "to": today - timedelta(days=1)},
        "method": "robust z-score (median/MAD, threshold 3.5) for absence; >=40% of a team on leave in a week for concentration; "
                  "rule checks for inconsistencies",
        "baseline": baseline,
        "unusual_absence": absence,
        "leave_concentration_weeks": conc,
        "inconsistencies": {
            "on_leave_without_approved_request": leave_no_req,
            "present_during_approved_leave": present_on_leave,
            "reporting_to_inactive_manager": orphan,
        },
    }
