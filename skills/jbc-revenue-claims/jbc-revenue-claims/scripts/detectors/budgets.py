"""Compound budget detector — emits four codes:

service-against-no-plan   (critical)  Service for a participant who has no
                                      plan/budget loaded.
plan-expired              (critical)  Service date after plan end date.
budget-approaching        (warning)   Utilisation >= REVENUE_BUDGET_WARNING_PCT
                                      (default 85%).
budget-exhausted          (critical)  Utilisation >= 100%.

SaH uses MONTHLY pool windows. NDIS uses plan-window. Do NOT unify
(per feedback_sah_pool_monthly).

In v0.1.0 ParticipantBudget is not yet surfaced via a config table.
Same pattern as pricing.py — when `budgets_by_participant` is empty,
a single info finding is emitted and we exit. Real wiring is a
straight-through callsite change in run_revenue_claims.py.

is_people_flag = TRUE on every emitted finding.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable


def _parse_iso(s: str | None) -> _dt.date | None:
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _period_key(program: str, svc_date: _dt.date) -> str:
    """SaH: monthly window key 'YYYY-MM'. NDIS: plan-window — caller
    supplies the plan key; we fall back to YYYY-MM if absent."""
    return svc_date.strftime("%Y-%m")


def run_budgets(
    entity: str,
    services: Iterable[Any],
    *,
    budgets_by_participant: dict[str, dict[str, Any]] | None = None,
    warning_pct: float = 85.0,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    budgets = budgets_by_participant or {}

    if not budgets:
        findings.append({
            "detector": "service-against-no-plan",
            "domain": "revenue",
            "severity": "info",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: participant budgets not loaded — budget checks skipped",
            "detail": (
                "ParticipantBudget / PricingRuleSet plan registry is not "
                "yet surfaced via a config table. service-against-no-plan, "
                "plan-expired, budget-approaching and budget-exhausted "
                "will not fire until budgets are loaded. Tracking ref: "
                "SKILL.md 'Deliberately skipped'."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"budget-registry-missing:{entity}",
                "kind": "budget-registry-missing",
            },
        })
        return findings

    # Accumulate spend per (participantRef, periodKey).
    spend: dict[tuple[str, str], float] = {}

    for svc in services:
        if getattr(svc, "entity_code", "") != entity:
            continue
        pref = getattr(svc, "participant_ref", "") or ""
        if not pref:
            continue
        program = getattr(svc, "program", "") or ""
        svc_date = _parse_iso(getattr(svc, "service_date_iso", "") or "")
        if not svc_date:
            continue

        budget = budgets.get(pref)
        if not budget:
            findings.append({
                "detector": "service-against-no-plan",
                "domain": "revenue",
                "severity": "critical",
                "entity_code": entity,
                "is_people_flag": True,
                "title": f"{entity} {program or '?'}: service for {pref} with no plan loaded",
                "detail": (
                    f"Delivered service on {svc_date.isoformat()} for {pref} "
                    f"({program or 'program unknown'}) — no ParticipantBudget "
                    f"record. Either the plan is missing from the loader or "
                    f"the participant is unfunded."
                ),
                "amount": getattr(svc, "line_total", None),
                "evidence": {
                    "dedupKey": f"service-against-no-plan:{entity}:{pref}:{svc_date.isoformat()}",
                    "kind": "service-against-no-plan",
                    "participantRef": pref,
                    "program": program,
                    "serviceDate": svc_date.isoformat(),
                },
            })
            continue

        plan_end = _parse_iso(budget.get("planEnd"))
        if plan_end and svc_date > plan_end:
            findings.append({
                "detector": "plan-expired",
                "domain": "revenue",
                "severity": "critical",
                "entity_code": entity,
                "is_people_flag": True,
                "title": f"{entity} {program or '?'}: plan expired before service for {pref}",
                "detail": (
                    f"Service on {svc_date.isoformat()} for {pref} occurred "
                    f"after plan end {plan_end.isoformat()}. Claim will be "
                    f"rejected — review immediately."
                ),
                "amount": getattr(svc, "line_total", None),
                "evidence": {
                    "dedupKey": f"plan-expired:{entity}:{pref}:{budget.get('planId', '?')}",
                    "kind": "plan-expired",
                    "participantRef": pref,
                    "planId": budget.get("planId"),
                    "planEnd": plan_end.isoformat(),
                    "serviceDate": svc_date.isoformat(),
                },
            })

        # Accumulate spend for budget-approaching/exhausted.
        period_key = _period_key(program, svc_date) if program == "SAH" else (
            budget.get("planId") or _period_key(program, svc_date)
        )
        amt = getattr(svc, "line_total", None)
        if amt is None:
            up = getattr(svc, "unit_price", None) or 0.0
            hrs = getattr(svc, "hours", 0.0) or 0.0
            amt = (up * hrs) if (up and hrs) else 0.0
        spend[(pref, period_key)] = spend.get((pref, period_key), 0.0) + float(amt or 0.0)

    # Utilisation pass.
    for (pref, period_key), spent in spend.items():
        budget = budgets.get(pref) or {}
        if not budget:
            continue
        # SaH uses monthly pool; NDIS uses plan total. The shape lives in
        # the caller-supplied dict — we read whichever the caller put in.
        cap = budget.get("monthlyPoolAud") if period_key == _dt.date.today().strftime("%Y-%m") \
            else None
        if cap is None:
            cap = budget.get("planTotalAud")
        if not cap or cap <= 0:
            continue
        util = (spent / float(cap)) * 100.0
        if util >= 100.0:
            findings.append({
                "detector": "budget-exhausted",
                "domain": "revenue",
                "severity": "critical",
                "entity_code": entity,
                "is_people_flag": True,
                "title": f"{entity}: budget exhausted for {pref} ({util:.0f}%)",
                "detail": (
                    f"Period {period_key}: spent ${spent:.2f} of ${cap:.2f}. "
                    f"Stop delivering billable services until plan is "
                    f"reviewed / topped up."
                ),
                "amount": round(spent - float(cap), 2),
                "evidence": {
                    "dedupKey": f"budget-exhausted:{entity}:{pref}:{period_key}",
                    "kind": "budget-exhausted",
                    "participantRef": pref,
                    "period": period_key,
                    "spent": round(spent, 2),
                    "cap": float(cap),
                    "utilisationPct": round(util, 1),
                },
            })
        elif util >= warning_pct:
            findings.append({
                "detector": "budget-approaching",
                "domain": "revenue",
                "severity": "warning",
                "entity_code": entity,
                "is_people_flag": True,
                "title": f"{entity}: budget approaching for {pref} ({util:.0f}%)",
                "detail": (
                    f"Period {period_key}: spent ${spent:.2f} of ${cap:.2f} "
                    f"({util:.1f}%). Threshold {warning_pct:.0f}%. Coordinate "
                    f"with the participant before rostering further hours."
                ),
                "amount": round(spent, 2),
                "evidence": {
                    "dedupKey": f"budget-approaching:{entity}:{pref}:{period_key}",
                    "kind": "budget-approaching",
                    "participantRef": pref,
                    "period": period_key,
                    "spent": round(spent, 2),
                    "cap": float(cap),
                    "utilisationPct": round(util, 1),
                    "warningPct": warning_pct,
                },
            })

    return findings
