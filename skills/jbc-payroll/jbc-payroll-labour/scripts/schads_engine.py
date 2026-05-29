"""SCHADS deterministic recompute (best-effort port).

The full Next.js engine (`lib/award/engine.ts`) handles classification ×
employment type rate lookup, dominant penalty multiplier across shift
windows, minimum engagement floors, overtime bands, and allowances.

The Python port is intentionally a thin shell — enough to:
  - Load an `AwardRuleSet` JSON if the user supplies one
    (SCHADS_RULESET_PATH).
  - Produce a `PayLineCheckResult` per MYOB pay line with status
    `match` | `variance` | `unverified`.

Without a ruleset, every line is `unverified` (the safe default per the
spec: never silently assume match).

Ruleset JSON shape (matches the legacy Prisma JSON):
{
  "version": "schads-2025-07",
  "effectiveFrom": "2025-07-01",
  "effectiveTo": null,
  "superRate": 0.12,
  "rates": {
    "Home Care Employee Level 2": {
      "permanent": 32.21,
      "casual": 40.26
    }
  },
  "penalty": {
    "saturday": 1.5, "sunday": 2.0, "public-holiday": 2.5,
    "evening": 1.125
  },
  "minimumEngagement": {"casual": 2.0, "permanent": 1.0}
}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class PayLineCheckResult:
    entity_code: str
    employee_id: str
    employee_name: str
    pay_run_id: str
    line_type: str
    paid: float
    computed: float
    variance: float
    status: str  # match | variance | unverified
    note: str | None = None
    shift_ref: str | None = None


def load_ruleset(path: str | None) -> dict[str, Any] | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _ordinary_line_types() -> set[str]:
    return {"ordinary", "ordinary-hours", "base", "wages"}


def _is_super(line_type: str) -> bool:
    return line_type in {"super", "ee-super", "er-super", "employer-super"}


def recompute(lines: Iterable[Any], ruleset: dict[str, Any] | None
              ) -> list[PayLineCheckResult]:
    """Per-line recompute.

    With NO ruleset every non-aggregate line is `unverified`. With a
    ruleset, ordinary-hours lines are recomputed as
    ``rate * hours * (1 if permanent else casual_loading)``; everything
    else is still `unverified` until the full penalty/allowance engine
    is ported.
    """
    out: list[PayLineCheckResult] = []
    ordinary = _ordinary_line_types()

    rates: dict[str, dict[str, float]] = {}
    if ruleset:
        rates = ruleset.get("rates") or {}

    for ln in lines:
        # Skip aggregate / control lines — super handled by detector.
        if _is_super(ln.line_type):
            continue
        if ln.line_type in {"payg", "post-tax-ded", "pre-tax-ded",
                            "net-pay", "gross"}:
            # MYOB summary aggregates; not pay-line checks.
            continue

        paid = float(ln.amount or 0.0)
        if ruleset and ln.line_type in ordinary and ln.classification \
                and ln.hours is not None:
            rate_row = rates.get(ln.classification)
            if rate_row:
                emp_type = (ln.employment_type or "permanent").lower()
                rate = rate_row.get(emp_type) or rate_row.get("permanent")
                if rate is not None:
                    computed = round(rate * float(ln.hours), 2)
                    variance = round(paid - computed, 2)
                    status = "match" if abs(variance) < 0.01 else "variance"
                    out.append(PayLineCheckResult(
                        entity_code=ln.entity_code,
                        employee_id=ln.employee_id,
                        employee_name=ln.employee_name,
                        pay_run_id=ln.pay_run_id,
                        line_type=ln.line_type,
                        paid=paid,
                        computed=computed,
                        variance=variance,
                        status=status,
                        note=f"rate={rate} hours={ln.hours} type={emp_type}",
                        shift_ref=ln.shift_ref,
                    ))
                    continue

        out.append(PayLineCheckResult(
            entity_code=ln.entity_code,
            employee_id=ln.employee_id,
            employee_name=ln.employee_name,
            pay_run_id=ln.pay_run_id,
            line_type=ln.line_type,
            paid=paid,
            computed=paid,
            variance=0.0,
            status="unverified",
            note=("no ruleset" if not ruleset else
                  "missing classification/hours or rate not in ruleset"),
            shift_ref=ln.shift_ref,
        ))
    return out
