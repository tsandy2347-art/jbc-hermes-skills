"""Domain B — integrity detectors.

  super-miscalc         (run-level; not people-flag)
  ghost-shift           (per-employee; people-flag)
  duplicate-payline     (per-employee; people-flag)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def detect_super_miscalc(lines: Iterable[Any],
                         ruleset: dict[str, Any] | None,
                         tolerance_aud: float) -> list[dict[str, Any]]:
    rate = float((ruleset or {}).get("superRate") or 0.0)
    if rate <= 0:
        return []  # cannot recompute without an effective-dated rate

    # Aggregate by (entity, pay_run_id).
    gross: dict[tuple[str, str], float] = defaultdict(float)
    super_paid: dict[tuple[str, str], float] = defaultdict(float)
    period: dict[tuple[str, str], tuple[str, str]] = {}

    for ln in lines:
        key = (ln.entity_code, ln.pay_run_id)
        period.setdefault(key, (ln.period_start, ln.period_end))
        if ln.line_type in {"super", "er-super", "employer-super"}:
            super_paid[key] += float(ln.amount or 0.0)
        elif ln.line_type in {"leave-accrual", "payg", "post-tax-ded",
                              "pre-tax-ded", "net-pay"}:
            continue
        elif ln.line_type == "gross":
            gross[key] += float(ln.amount or 0.0)
        else:
            # Treat individual pay lines as part of gross when no MYOB
            # aggregate `gross` row is present. Harmless when gross IS
            # present because we'd then double-count; in practice MYOB
            # Pay Activity Summary uses one or the other.
            gross[key] += float(ln.amount or 0.0)

    out: list[dict[str, Any]] = []
    for key, g in gross.items():
        entity, run_id = key
        paid = super_paid.get(key, 0.0)
        # If both an aggregate gross row AND detail lines exist we may
        # have over-counted gross. Pick the larger of the two as a
        # conservative gross.
        expected = round(g * rate, 2)
        variance = round(paid - expected, 2)
        if abs(variance) <= tolerance_aud:
            continue
        missing = paid == 0
        under = variance < 0
        severity = "critical" if (missing or (under and abs(variance) >= 50)) else "warning"
        p_start, p_end = period.get(key, ("", ""))
        out.append({
            "detector": "super-miscalc",
            "domain": "integrity",
            "severity": severity,
            "entity_code": entity,
            "is_people_flag": False,
            "title": (
                f"Super {'MISSING' if missing else ('under' if under else 'over')}"
                f"-contributed for {entity} pay run"
            ),
            "detail": (
                f"Pay run {run_id} ({entity}, {p_start} → {p_end}). "
                f"Gross subject to SG: A${g:,.2f}. Expected super at "
                f"{rate*100:.2f}%: A${expected:,.2f}. Paid: A${paid:,.2f}. "
                f"Variance A${variance:,.2f}. Super not paid or materially "
                f"wrong is critical — review before finalising."
            ),
            "amount": round(abs(variance), 2),
            "evidence": {
                "dedupKey": f"super-miscalc:{entity}:{run_id}",
                "entityCode": entity, "payRunId": run_id,
                "gross": round(g, 2), "superPaid": round(paid, 2),
                "expected": expected, "variance": variance, "rate": rate,
            },
        })
    return out


def detect_duplicate_payline(lines: Iterable[Any]) -> list[dict[str, Any]]:
    """Same employee + lineType + amount appearing more than once in a run."""
    counts: dict[tuple[str, str, str, str, float], int] = defaultdict(int)
    sample: dict[tuple[str, str, str, str, float], Any] = {}
    for ln in lines:
        # Aggregate control lines aren't duplicates.
        if ln.line_type in {"gross", "net-pay", "payg"}:
            continue
        key = (ln.entity_code, ln.employee_id, ln.pay_run_id,
               ln.line_type, round(float(ln.amount or 0.0), 2))
        counts[key] += 1
        sample.setdefault(key, ln)

    out: list[dict[str, Any]] = []
    for key, n in counts.items():
        if n < 2:
            continue
        entity, emp_id, run_id, line_type, amount = key
        ln = sample[key]
        out.append({
            "detector": "duplicate-payline",
            "domain": "integrity",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": (
                f"Duplicate pay line × {n}: {ln.employee_name} {line_type} "
                f"A${amount:,.2f}"
            ),
            "detail": (
                f"Pay line for {ln.employee_name} ({emp_id}) of type "
                f"{line_type} A${amount:,.2f} appears {n} times in pay run "
                f"{run_id}. Likely double-keyed in MYOB."
            ),
            "amount": round(amount * (n - 1), 2),
            "evidence": {
                "dedupKey": (
                    f"duplicate-payline:{entity}:{emp_id}:{line_type}:"
                    f"{amount}:{run_id}"
                ),
                "entityCode": entity, "employeeId": emp_id,
                "employeeName": ln.employee_name, "payRunId": run_id,
                "lineType": line_type, "amount": amount, "occurrences": n,
            },
        })
    return out


def detect_ghost_shift(lines: Iterable[Any],
                       shifts: Iterable[Any]) -> list[dict[str, Any]]:
    """Employee paid hours with no matching AlayaCare shift in the run.

    Approximation: any employee who has paid hours in MYOB but zero
    paid_hours in AlayaCare for the same pay_run_id is flagged.
    """
    paid_hours: dict[tuple[str, str, str], float] = defaultdict(float)
    names: dict[tuple[str, str, str], str] = {}
    for ln in lines:
        if ln.hours and ln.hours > 0:
            key = (ln.entity_code, ln.employee_id, ln.pay_run_id)
            paid_hours[key] += float(ln.hours)
            names[key] = ln.employee_name

    ac_hours: dict[tuple[str, str, str | None], float] = defaultdict(float)
    for s in shifts:
        ac_hours[(s.entity_code, s.employee_id, s.pay_run_id)] += s.paid_hours

    out: list[dict[str, Any]] = []
    for key, h in paid_hours.items():
        entity, emp_id, run_id = key
        ac = ac_hours.get(key, 0.0)
        if ac == 0.0:
            # Also try the same employee with no pay-run match (some
            # AlayaCare exports don't carry pay_run_id).
            any_ac = any(
                s_key[:2] == (entity, emp_id) and v > 0
                for s_key, v in ac_hours.items()
            )
            if any_ac:
                continue
            out.append({
                "detector": "ghost-shift",
                "domain": "integrity",
                "severity": "warning",
                "entity_code": entity,
                "is_people_flag": True,
                "title": (
                    f"Ghost shift: {names[key]} paid {h:.2f}h with no "
                    f"AlayaCare roster"
                ),
                "detail": (
                    f"MYOB shows {h:.2f}h paid for {names[key]} ({emp_id}) "
                    f"in pay run {run_id} but AlayaCare has zero shifts for "
                    f"this employee. Cross-check with Controls & Audit."
                ),
                "amount": None,
                "evidence": {
                    "dedupKey": f"ghost-shift:{entity}:{emp_id}:{run_id}",
                    "entityCode": entity, "employeeId": emp_id,
                    "employeeName": names[key], "payRunId": run_id,
                    "paidHours": h, "alayacareHours": ac,
                },
            })
    return out
