"""Domain A — award detectors.

  pay-line-variance       (people-flag, critical if underpaid >= $50)
  unverified-line         (people-flag, warning — needs human review)
  systemic-underpayment   (not people-flag; pattern across staff)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def detect_pay_line_variance(line_checks: Iterable[Any],
                             tolerance_aud: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in line_checks:
        if c.status != "variance":
            continue
        if abs(c.variance) <= tolerance_aud:
            continue
        underpaid = c.variance < 0
        amount = round(abs(c.variance), 2)
        severity = "critical" if (underpaid and amount >= 50) else "warning"
        out.append({
            "detector": "pay-line-variance",
            "domain": "award",
            "severity": severity,
            "entity_code": c.entity_code,
            "is_people_flag": True,
            "title": (
                f"{'Underpaid' if underpaid else 'Overpaid'} "
                f"{c.line_type} — {c.employee_name} (A${amount:,.2f})"
            ),
            "detail": (
                f"Pay line for {c.employee_name} ({c.employee_id}) on "
                f"{c.line_type}: paid A${c.paid:,.2f}, engine computed "
                f"A${c.computed:,.2f} (variance A${c.variance:,.2f}). "
                f"{c.note or ''}"
            ).strip(),
            "amount": amount,
            "evidence": {
                "dedupKey": (
                    f"pay-line-variance:{c.entity_code}:{c.employee_id}:"
                    f"{c.pay_run_id}:{c.line_type}"
                ),
                "entityCode": c.entity_code,
                "employeeId": c.employee_id,
                "employeeName": c.employee_name,
                "payRunId": c.pay_run_id,
                "lineType": c.line_type,
                "paid": c.paid,
                "computed": c.computed,
                "variance": c.variance,
                "shiftRef": c.shift_ref,
                "note": c.note,
            },
        })
    return out


def detect_unverified_line(line_checks: Iterable[Any]) -> list[dict[str, Any]]:
    """Surface engine-unverified lines for human review.

    Emitted at warning severity. Aggregated per (entity, employee,
    pay_run_id, line_type) — one finding per group with a count.
    """
    groups: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
    for c in line_checks:
        if c.status != "unverified":
            continue
        groups[(c.entity_code, c.employee_id, c.pay_run_id, c.line_type)].append(c)

    out: list[dict[str, Any]] = []
    for (entity, emp_id, run_id, line_type), rows in groups.items():
        sample = rows[0]
        total = round(sum(r.paid for r in rows), 2)
        out.append({
            "detector": "unverified-line",
            "domain": "award",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": (
                f"Unverified {line_type} × {len(rows)} for "
                f"{sample.employee_name} (A${total:,.2f})"
            ),
            "detail": (
                f"SCHADS engine could not verify {len(rows)} {line_type} "
                f"line(s) for {sample.employee_name} ({emp_id}) in pay run "
                f"{run_id}. Reason: {sample.note or 'unknown'}. Human review "
                f"required — never silently assume match."
            ),
            "amount": total,
            "evidence": {
                "dedupKey": (
                    f"unverified-line:{entity}:{emp_id}:{run_id}:{line_type}"
                ),
                "entityCode": entity,
                "employeeId": emp_id,
                "employeeName": sample.employee_name,
                "payRunId": run_id,
                "lineType": line_type,
                "lineCount": len(rows),
                "totalPaid": total,
                "note": sample.note,
            },
        })
    return out


def detect_systemic_underpayment(line_checks: Iterable[Any],
                                 min_staff: int) -> list[dict[str, Any]]:
    """Same lineType underpaid across >= min_staff distinct employees in one run.

    Critical — legal exposure. Not a people-flag (pattern across staff,
    not a named individual).
    """
    bucket: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    totals: dict[tuple[str, str, str], float] = defaultdict(float)
    for c in line_checks:
        if c.status != "variance" or c.variance >= 0:
            continue
        key = (c.entity_code, c.pay_run_id, c.line_type)
        bucket[key].add((c.employee_id, c.employee_name))
        totals[key] += abs(c.variance)

    out: list[dict[str, Any]] = []
    for (entity, run_id, line_type), staff in bucket.items():
        if len(staff) < min_staff:
            continue
        total = round(totals[(entity, run_id, line_type)], 2)
        out.append({
            "detector": "systemic-underpayment",
            "domain": "award",
            "severity": "critical",
            "entity_code": entity,
            "is_people_flag": False,
            "title": (
                f"Systemic underpayment: {line_type} × {len(staff)} staff "
                f"({entity}, A${total:,.2f})"
            ),
            "detail": (
                f"{len(staff)} distinct staff in pay run {run_id} were "
                f"underpaid on {line_type}. Aggregate shortfall "
                f"A${total:,.2f}. Australian underpayment carries serious "
                f"legal exposure — same-day human action required."
            ),
            "amount": total,
            "evidence": {
                "dedupKey": f"systemic-underpayment:{entity}:{line_type}:{run_id}",
                "entityCode": entity,
                "payRunId": run_id,
                "lineType": line_type,
                "staffCount": len(staff),
                "totalShortfall": total,
            },
        })
    return out
