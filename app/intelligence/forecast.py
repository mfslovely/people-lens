"""Availability forecasting that keeps facts and predictions separate.

expected_available(d) = headcount - scheduled_leave(d) [fact: approved + pending requests]
                                  - predicted_unplanned(d) [model]

Unplanned-absence model: weekday-specific rate of 'absent' plus same-day sick leave from the last 8 weeks
(weekday seasonality), multiplied by the employees not already scheduled to be away.
Baseline: a flat overall rate (no weekday effect).
Evaluation: a rolling backtest over the last 4 weeks, fitting on the 8 weeks before each target week. We report the
mean absolute error (people per day) of both, so the model is only trusted if it beats the baseline.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import numpy as np

from app.core.dates import working_days


def _daily_unplanned(conn, ids: list[int], start: date, end: date) -> dict[date, int]:
    rows = conn.execute(
        """SELECT a.date, count(*) AS n FROM hr.v_attendance_scoped a
            WHERE a.employee_id = ANY(%s) AND a.date BETWEEN %s AND %s AND a.status = 'absent' GROUP BY 1""",
        (ids, start, end)).fetchall()
    sick = conn.execute(
        """SELECT d::date AS date, count(*) AS n FROM hr.v_leave_scoped l
            CROSS JOIN LATERAL generate_series(l.start_date, l.end_date, interval '1 day') d
            WHERE l.employee_id = ANY(%s) AND l.status='approved' AND l.leave_type='SL'
              AND (l.created_at::date >= l.start_date - 1) AND d::date BETWEEN %s AND %s GROUP BY 1""",
        (ids, start, end)).fetchall()
    out: dict[date, int] = defaultdict(int)
    for r in rows + sick:
        out[r["date"]] += r["n"]
    return out


def _fit(daily: dict[date, int], days: list[date], n: int) -> tuple[dict[int, float], float]:
    by_wd: dict[int, list[float]] = defaultdict(list)
    for d in days:
        by_wd[d.weekday()].append(daily.get(d, 0) / n)
    flat = float(np.mean([daily.get(d, 0) / n for d in days])) if days else 0.0
    return {wd: float(np.mean(v)) for wd, v in by_wd.items()}, flat


def run(conn, today: date, start: date, end: date, department: str | None) -> dict:
    hol = {r["date"] for r in conn.execute("SELECT date FROM hr.holidays").fetchall()}
    q = "SELECT id FROM hr.v_employees_scoped WHERE status='active'" + (" AND department ILIKE %s" if department else "")
    ids = [r["id"] for r in conn.execute(q, (department,) if department else None).fetchall()]
    if not ids:
        raise ValueError("No active employees in scope for this forecast.")
    n = len(ids)

    hist_start = today - timedelta(days=12 * 7)
    daily = _daily_unplanned(conn, ids, hist_start, today - timedelta(days=1))

    # backtest: last 4 weeks, each predicted from the preceding 8 weeks
    errs_model, errs_base = [], []
    for w in range(4, 0, -1):
        tgt_start = today - timedelta(days=7 * w)
        train = working_days(tgt_start - timedelta(days=56), tgt_start - timedelta(days=1), hol)
        rates, flat = _fit(daily, train, n)
        for d in working_days(tgt_start, tgt_start + timedelta(days=6), hol):
            if d >= today:
                continue
            actual = daily.get(d, 0)
            errs_model.append(abs(actual - rates.get(d.weekday(), flat) * n))
            errs_base.append(abs(actual - flat * n))
    rates, flat = _fit(daily, working_days(today - timedelta(days=56), today - timedelta(days=1), hol), n)
    mae_m = round(float(np.mean(errs_model)), 2) if errs_model else None
    mae_b = round(float(np.mean(errs_base)), 2) if errs_base else None
    use_model = mae_m is not None and mae_b is not None and mae_m <= mae_b

    sched = conn.execute(
        """SELECT employee_id, start_date, end_date, status FROM hr.v_leave_scoped
            WHERE employee_id = ANY(%s) AND status IN ('approved','pending')
              AND daterange(start_date,end_date,'[]') && daterange(%s,%s,'[]')""", (ids, start, end)).fetchall()
    days = []
    for d in working_days(start, end, hol):
        appr = {s["employee_id"] for s in sched if s["status"] == "approved" and s["start_date"] <= d <= s["end_date"]}
        pend = {s["employee_id"] for s in sched if s["status"] == "pending" and s["start_date"] <= d <= s["end_date"]} - appr
        remaining = n - len(appr) - len(pend)
        rate = rates.get(d.weekday(), flat) if use_model else flat
        pred = round(rate * remaining, 1)
        days.append({"date": d, "weekday": d.strftime("%A"), "headcount": n,
                     "scheduled_approved_leave": len(appr), "scheduled_pending_leave": len(pend),
                     "predicted_unplanned_absence": pred,
                     "expected_available": round(remaining - pred, 1),
                     "expected_available_pct": round(100 * (remaining - pred) / n, 1)})
    return {
        "scope_headcount": n,
        "labels": {"scheduled_*": "FACT: from leave requests", "predicted_unplanned_absence": "PREDICTION: statistical model"},
        "model": "weekday-seasonal unplanned-absence rate (8-week window)" if use_model else "flat baseline rate (model did not beat baseline)",
        "evaluation": {"method": "rolling 4-week backtest, 8-week training window", "mae_people_per_day_model": mae_m,
                       "mae_people_per_day_baseline": mae_b, "points": len(errs_model)},
        "days": days,
    }
