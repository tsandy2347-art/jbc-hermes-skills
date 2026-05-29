"""BAS deadline detector — Domain B.

Emits warning/critical findings when an upcoming BAS due date is inside
the configured window. The legacy `bas-ready` detector required a
BasPreparation side-table; we drop that and rely solely on the deadline
clock — humans know whether the return is prepared.
"""

from __future__ import annotations

import os
from typing import Any

from scripts.jbc_tax_rulesets import (
    DEFAULT_DUE_DATE_CRITICAL_DAYS,
    DEFAULT_DUE_DATE_WARNING_DAYS,
    ruleset_meta,
)
from scripts.periods import days_until, today_bne, upcoming_bas_periods
from scripts import xero_tax


def _bas_cycle(entity: str) -> str:
    return os.environ.get(f"TAX_BAS_CYCLE_{entity}", "quarterly")


def run_bas(entity: str) -> list[dict[str, Any]]:
    if not xero_tax.tenant_configured(entity):
        return []

    crit_days = int(os.environ.get(
        "TAX_DUE_DATE_CRITICAL_DAYS", DEFAULT_DUE_DATE_CRITICAL_DAYS,
    ))
    warn_days = int(os.environ.get(
        "TAX_DUE_DATE_WARNING_DAYS", DEFAULT_DUE_DATE_WARNING_DAYS,
    ))
    today = today_bne()
    horizon = warn_days + 7
    periods = upcoming_bas_periods(today, _bas_cycle(entity), horizon)
    meta = ruleset_meta()

    out: list[dict[str, Any]] = []
    for p in periods:
        d_left = days_until(p.due_date, today)
        if d_left < 0 or d_left > warn_days:
            continue
        severity = "critical" if d_left <= crit_days else "warning"
        out.append({
            "detector": "bas-deadline",
            "domain": "bas",
            "severity": severity,
            "entity_code": entity,
            "title": f"[{entity}] BAS due in {d_left} day(s) — {p.label}",
            "detail": (
                f"{entity} BAS for {p.label} is due on {p.due_date.isoformat()} "
                f"({d_left} day(s) from now). The skill emits the live GST position "
                f"separately — review against that figure. Lodgement is performed "
                f"by Nicole / the external accountant; the skill never lodges."
            ),
            "evidence": {
                "dedupKey": f"bas-deadline:{entity}:{p.label}",
                "entityCode": entity,
                "period": {
                    "start": p.start.isoformat(),
                    "end": p.end.isoformat(),
                    "label": p.label,
                    "dueIso": p.due_date.isoformat(),
                },
                "daysLeft": d_left,
                **meta,
            },
        })
    return out
