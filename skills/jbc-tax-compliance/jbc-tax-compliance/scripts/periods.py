"""BAS quarterly / monthly period helpers for jbc-tax-compliance.

Brisbane local timezone, UTC+10, no DST. Standalone (no luxon equivalent
needed) — uses datetime + a fixed offset.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

BNE = _dt.timezone(_dt.timedelta(hours=10), name="Australia/Brisbane")


@dataclass
class Period:
    start: _dt.date         # inclusive
    end: _dt.date           # inclusive last day
    label: str
    due_date: _dt.date


def now_bne() -> _dt.datetime:
    return _dt.datetime.now(tz=BNE)


def today_bne() -> _dt.date:
    return now_bne().date()


def quarterly_bas_period_for(d: _dt.date) -> Period:
    """Return the BAS quarter the date falls into.

    Q1 Jul-Sep → due 28 Oct
    Q2 Oct-Dec → due 28 Feb (concession)
    Q3 Jan-Mar → due 28 Apr
    Q4 Apr-Jun → due 28 Jul
    """
    m = d.month
    if 7 <= m <= 9:
        start = _dt.date(d.year, 7, 1); end = _dt.date(d.year, 9, 30)
        due = _dt.date(d.year, 10, 28); q = 1
        fy_start, fy_end = d.year, d.year + 1
    elif 10 <= m <= 12:
        start = _dt.date(d.year, 10, 1); end = _dt.date(d.year, 12, 31)
        due = _dt.date(d.year + 1, 2, 28); q = 2
        fy_start, fy_end = d.year, d.year + 1
    elif 1 <= m <= 3:
        start = _dt.date(d.year, 1, 1); end = _dt.date(d.year, 3, 31)
        due = _dt.date(d.year, 4, 28); q = 3
        fy_start, fy_end = d.year - 1, d.year
    else:  # 4..6
        start = _dt.date(d.year, 4, 1); end = _dt.date(d.year, 6, 30)
        due = _dt.date(d.year, 7, 28); q = 4
        fy_start, fy_end = d.year - 1, d.year
    label = f"Q{q} FY{fy_start}-{str(fy_end)[-2:]}"
    return Period(start=start, end=end, label=label, due_date=due)


def monthly_bas_period_for(d: _dt.date) -> Period:
    start = _dt.date(d.year, d.month, 1)
    # last day of month
    nxt_month = d.month + 1
    nxt_year = d.year + (1 if nxt_month > 12 else 0)
    nxt_month = nxt_month - 12 if nxt_month > 12 else nxt_month
    end = _dt.date(nxt_year, nxt_month, 1) - _dt.timedelta(days=1)
    # monthly BAS due 21st of following month
    due = _dt.date(nxt_year, nxt_month, 21)
    label = f"{start.strftime('%b %Y')} (monthly)"
    return Period(start=start, end=end, label=label, due_date=due)


def bas_period_for(d: _dt.date, cycle: str) -> Period:
    if cycle.lower() == "monthly":
        return monthly_bas_period_for(d)
    return quarterly_bas_period_for(d)


def upcoming_bas_periods(start_date: _dt.date, cycle: str, horizon_days: int) -> list[Period]:
    out: list[Period] = []
    cur = bas_period_for(start_date, cycle)
    out.append(cur)
    end_horizon = start_date + _dt.timedelta(days=horizon_days)
    while cur.end < end_horizon:
        nxt_seed = cur.end + _dt.timedelta(days=1)
        cur = bas_period_for(nxt_seed, cycle)
        out.append(cur)
        if len(out) > 12:
            break
    return out


def days_until(target: _dt.date, _from: _dt.date | None = None) -> int:
    f = _from or today_bne()
    return (target - f).days
