"""Domain F — rostering detector.

  broken-shift-trigger   per-employee shift gap detected as avoidable.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable


def _parse(ts: str) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def detect_broken_shift_trigger(shifts: Iterable[Any]) -> list[dict[str, Any]]:
    """Emit one info finding per shift whose kind == 'broken-gap'.

    The source agent infers broken-shifts from start/end timestamps; for
    the read-only Hermes port we trust the upstream AlayaCare classification
    (`shift_kind == 'broken-gap'`). If no such rows exist, return [].
    """
    out: list[dict[str, Any]] = []
    for s in shifts:
        if s.shift_kind != "broken-gap":
            continue
        when = _parse(s.start_ts)
        out.append({
            "detector": "broken-shift-trigger",
            "domain": "rostering",
            "severity": "info",
            "entity_code": s.entity_code,
            "is_people_flag": True,
            "title": (
                f"Broken-shift gap: {s.employee_name} "
                f"{when.date().isoformat() if when else s.start_ts}"
            ),
            "detail": (
                f"Broken-shift allowance triggered for {s.employee_name} "
                f"({s.employee_id}) on shift {s.shift_id}. Roster review "
                f"may avoid the allowance next cycle."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": (
                    f"broken-shift-trigger:{s.entity_code}:{s.employee_id}:"
                    f"{s.shift_id}"
                ),
                "entityCode": s.entity_code,
                "employeeId": s.employee_id,
                "employeeName": s.employee_name,
                "shiftId": s.shift_id,
                "startTs": s.start_ts,
                "endTs": s.end_ts,
            },
        })
    return out
