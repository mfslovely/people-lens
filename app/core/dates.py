"""Deterministic natural-language date resolution. The LLM never does date arithmetic itself."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dateutil import parser as du_parser

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD_RE = r"(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_MONTHS = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def to_dict(self) -> dict:
        return {"start_date": self.start.isoformat(), "end_date": self.end.isoformat(),
                "start_weekday": self.start.strftime("%A"), "end_weekday": self.end.strftime("%A")}


def _wd_index(token: str) -> int:
    t = token.lower()[:3]
    return [w[:3] for w in WEEKDAYS].index(t)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _next_weekday(today: date, idx: int) -> date:
    delta = (idx - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _explicit_dates(text: str, today: date) -> list[date]:
    found: list[date] = []
    for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", text):
        found.append(date.fromisoformat(m.group(1)))
    pattern = rf"\b(\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}(?:\s+\d{{4}})?|{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?)\b"
    for m in re.finditer(pattern, text, flags=re.I):
        try:
            d = du_parser.parse(m.group(0), default=datetime(today.year, today.month, 1)).date()
        except (ValueError, OverflowError):
            continue
        if not re.search(r"\d{4}", m.group(0)) and d < today - timedelta(days=60):
            d = d.replace(year=d.year + 1)  # "3 Jan" in late November means next year
        found.append(d)
    return found


def resolve(expression: str, today: date) -> DateRange | None:
    """Resolve phrases like 'next Monday and Tuesday', 'next week', 'tomorrow', '2026-12-03 to 2026-12-05'."""
    text = expression.lower().strip()

    explicit = _explicit_dates(text, today)
    if explicit:
        m = re.search(r"for (\d+) (?:working )?days", text)
        if len(explicit) == 1 and m:
            return DateRange(explicit[0], add_working_days(explicit[0], int(m.group(1)) - 1, set()))
        return DateRange(min(explicit), max(explicit))

    if re.search(r"\bday after tomorrow\b", text):
        d = today + timedelta(days=2)
        return DateRange(d, d)
    if re.search(r"\btomorrow\b", text):
        d = today + timedelta(days=1)
        return DateRange(d, d)
    if re.search(r"\btoday\b", text):
        return DateRange(today, today)

    weekdays = [_wd_index(m.group(1)) for m in re.finditer(rf"\b{_WD_RE}\b", text)]
    next_week = bool(re.search(r"\bnext week\b", text))
    this_week = bool(re.search(r"\bthis week\b", text))

    if weekdays:
        if next_week:
            base = week_start(today) + timedelta(days=7)
            days = [base + timedelta(days=i) for i in weekdays]
        elif this_week:
            base = week_start(today)
            days = [base + timedelta(days=i) for i in weekdays]
        else:
            first = _next_weekday(today, weekdays[0])
            days = [first]
            for idx in weekdays[1:]:
                d = first + timedelta(days=(idx - first.weekday()) % 7)
                days.append(d)
        if " to " in text or " through " in text or "-" in text or " until " in text:
            return DateRange(min(days), max(days))
        return DateRange(min(days), max(days))

    m = re.search(r"\bnext (\d+) (?:working )?days\b", text)
    if m:
        start = today + timedelta(days=1)
        return DateRange(start, add_working_days(start, int(m.group(1)) - 1, set()))
    if next_week:
        base = week_start(today) + timedelta(days=7)
        return DateRange(base, base + timedelta(days=4))
    if this_week:
        base = week_start(today)
        return DateRange(base, base + timedelta(days=4))
    if re.search(r"\bnext month\b", text):
        first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return DateRange(first, last)
    if re.search(r"\b(last|past) (three|3) months\b", text):
        return DateRange(today - timedelta(days=91), today)
    if re.search(r"\blast month\b", text):
        last = today.replace(day=1) - timedelta(days=1)
        return DateRange(last.replace(day=1), last)
    return None


def is_working_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def working_days(start: date, end: date, holidays: set[date]) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_working_day(d, holidays):
            out.append(d)
        d += timedelta(days=1)
    return out


def add_working_days(start: date, n: int, holidays: set[date]) -> date:
    d = start
    while not is_working_day(d, holidays):
        d += timedelta(days=1)
    while n > 0:
        d += timedelta(days=1)
        if is_working_day(d, holidays):
            n -= 1
    return d
