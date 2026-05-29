"""Domain C/D/E — labour analytics detectors.

  labour-cost-pct    aggregate; not people-flag
  utilisation-drop   aggregate; not people-flag
  overtime-spike     aggregate; not people-flag
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _total_labour_cost(lines: Iterable[Any], entity: str) -> float:
    """Sum cash earnings + employer super for the entity. Excludes deductions."""
    total = 0.0
    for ln in lines:
        if ln.entity_code != entity:
            continue
        if ln.line_type in {"payg", "post-tax-ded", "pre-tax-ded", "net-pay"}:
            continue
        if ln.line_type == "gross":
            total += float(ln.amount or 0.0)
        elif ln.line_type in {"er-super", "employer-super", "super"}:
            total += float(ln.amount or 0.0)
        elif ln.line_type in {"ee-super"}:
            # Salary sacrifice — already inside gross per Craig pattern.
            continue
        else:
            total += float(ln.amount or 0.0)
    return round(total, 2)


def detect_labour_cost_pct(lines: Iterable[Any],
                           revenue: dict[str, float | None],
                           targets: dict[str, float]) -> list[dict[str, Any]]:
    lines = list(lines)
    out: list[dict[str, Any]] = []
    today_iso_period = "current"
    for entity in ("SC", "CQ"):
        rev = revenue.get(entity)
        if not rev or rev <= 0:
            continue
        cost = _total_labour_cost(lines, entity)
        if cost == 0:
            continue
        pct = round(100.0 * cost / rev, 2)
        target = targets.get(entity, 70.0)
        if pct > target + 5:
            severity = "warning"
        elif pct > target:
            severity = "info"
        else:
            severity = "info"
        out.append({
            "detector": "labour-cost-pct",
            "domain": "labour-cost",
            "severity": severity,
            "entity_code": entity,
            "is_people_flag": False,
            "title": (
                f"Labour cost {pct:.1f}% of revenue ({entity}, target {target:.0f}%)"
            ),
            "detail": (
                f"Total labour cost A${cost:,.2f} on revenue A${rev:,.2f} → "
                f"{pct:.2f}%. Target {targets.get(entity):.0f}%. "
                f"{'Above target — investigate mix/utilisation.' if pct > target else 'Within target.'}"
            ),
            "amount": cost,
            "evidence": {
                "dedupKey": f"labour-cost-pct:{entity}:{today_iso_period}",
                "entityCode": entity, "labourCost": cost, "revenue": rev,
                "pct": pct, "target": targets.get(entity),
            },
        })
    return out


def detect_utilisation_drop(shifts: Iterable[Any],
                            floor_pct: float) -> list[dict[str, Any]]:
    paid: dict[str, float] = defaultdict(float)
    billable: dict[str, float] = defaultdict(float)
    for s in shifts:
        paid[s.entity_code] += s.paid_hours
        billable[s.entity_code] += s.billable_hours
    out: list[dict[str, Any]] = []
    for entity, p in paid.items():
        if p <= 0:
            continue
        b = billable.get(entity, 0.0)
        pct = round(100.0 * b / p, 2)
        if pct >= floor_pct:
            continue
        out.append({
            "detector": "utilisation-drop",
            "domain": "utilisation",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": (
                f"Utilisation {pct:.1f}% below floor {floor_pct:.0f}% ({entity})"
            ),
            "detail": (
                f"Billable {b:.1f}h ÷ paid {p:.1f}h = {pct:.1f}%. Gap "
                f"({p-b:.1f}h) is unbillable time — travel, gaps, training."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"utilisation-drop:{entity}:current",
                "entityCode": entity, "paidHours": p, "billableHours": b,
                "pct": pct, "floor": floor_pct,
            },
        })
    return out


def detect_overtime_spike(lines: Iterable[Any],
                          alert_pct: float) -> list[dict[str, Any]]:
    lines = list(lines)
    out: list[dict[str, Any]] = []
    for entity in ("SC", "CQ"):
        ot = 0.0
        total = 0.0
        run_id = None
        for ln in lines:
            if ln.entity_code != entity:
                continue
            if ln.line_type in {"payg", "post-tax-ded", "pre-tax-ded",
                                "net-pay"}:
                continue
            run_id = run_id or ln.pay_run_id
            if "overtime" in ln.line_type or ln.line_type.startswith("ot"):
                ot += float(ln.amount or 0.0)
            total += float(ln.amount or 0.0)
        if total <= 0 or ot <= 0:
            continue
        pct = round(100.0 * ot / total, 2)
        if pct < alert_pct:
            continue
        out.append({
            "detector": "overtime-spike",
            "domain": "overtime",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"OT spend {pct:.1f}% of payroll ({entity}, alert {alert_pct:.0f}%)",
            "detail": (
                f"Overtime A${ot:,.2f} on payroll A${total:,.2f} → {pct:.2f}%. "
                f"Above alert threshold {alert_pct:.0f}% — investigate roster gaps."
            ),
            "amount": round(ot, 2),
            "evidence": {
                "dedupKey": f"overtime-spike:{entity}:{run_id or 'current'}",
                "entityCode": entity, "payRunId": run_id,
                "overtime": round(ot, 2), "payroll": round(total, 2),
                "pct": pct, "alertPct": alert_pct,
            },
        })
    return out
