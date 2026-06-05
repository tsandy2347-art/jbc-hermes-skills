"""QLD payroll-tax threshold detector — Domain C.

Pulls a 12-month wages position per entity from Xero's Profit & Loss
report (sum of wages-account balances over the last 365 days). Compares
against the QLD annual threshold from `jbc_tax_rulesets`.

GROUPING:
  - Default: per-entity threshold comparison.
  - If TAX_PAYROLL_TAX_GROUPED=true, sums SC+CQ wages and emits a single
    GROUPED finding (entity_code='GROUPED'). This is the ONLY consolidated
    finding the skill ever emits, and only because QLD payroll-tax
    grouping is a statutory regime that does combine entities.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from scripts import xero_tax
from scripts.jbc_tax_rulesets import (
    QLD_PAYROLL_TAX_ANNUAL_THRESHOLD_AUD,
    QLD_PAYROLL_TAX_RATE,
    ruleset_meta,
)
from scripts.periods import today_bne


def _grouped() -> bool:
    return os.environ.get("TAX_PAYROLL_TAX_GROUPED", "").lower() == "true"


def _wages_for(entity: str) -> tuple[float | None, list[str]]:
    """Return (12-month wages total, codes tried) for an entity, or (None, codes)."""
    codes = xero_tax.split_codes(
        os.environ.get(f"XERO_{entity}_WAGES_ACCOUNT_CODES")
    )
    if not codes:
        return None, []
    today = today_bne()
    one_year_ago = today - _dt.timedelta(days=365)
    pl = xero_tax.profit_and_loss(entity, one_year_ago.isoformat(),
                                  today.isoformat())
    if not pl:
        return None, codes
    accounts = xero_tax.list_accounts(entity)
    total = 0.0
    found = 0
    for c in codes:
        acc = next((a for a in accounts if a.get("Code") == c), None)
        name = acc.get("Name") if acc else None
        bal = xero_tax.find_account_balance(pl, c, name)
        if bal is None:
            continue
        found += 1
        total += abs(bal)
    if found == 0:
        return None, codes
    return round(total * 100) / 100, codes


def _payroll_tax_paid_ytd(entity: str) -> tuple[float | None, list[str]]:
    """Return (12-month payroll tax expense, codes tried) — i.e. what's been
    booked as a payroll-tax expense over the last 365 days. Whichever Xero
    bookkeeping pattern (direct-to-expense or via-liability-clearing) is used,
    the EXPENSE account ends up with the cost — so reading the P&L on that
    account gives us "paid + accrued" YTD, the closest thing to "what we've
    paid the QRO" without going to the QRO portal.
    """
    codes = xero_tax.split_codes(
        os.environ.get(f"XERO_{entity}_PAYROLL_TAX_EXPENSE_ACCOUNT_CODES")
    )
    if not codes:
        return None, []
    today = today_bne()
    one_year_ago = today - _dt.timedelta(days=365)
    pl = xero_tax.profit_and_loss(entity, one_year_ago.isoformat(), today.isoformat())
    if not pl:
        return None, codes
    accounts = xero_tax.list_accounts(entity)
    total = 0.0
    found = 0
    for c in codes:
        acc = next((a for a in accounts if a.get("Code") == c), None)
        name = acc.get("Name") if acc else None
        bal = xero_tax.find_account_balance(pl, c, name)
        if bal is None:
            continue
        found += 1
        total += abs(bal)
    if found == 0:
        return None, codes
    return round(total * 100) / 100, codes


def _emit(entity_code: str, wages: float, *, period_label: str,
          components: dict[str, float] | None = None,
          paid_ytd: float | None = None,
          paid_components: dict[str, float] | None = None) -> dict[str, Any]:
    threshold = QLD_PAYROLL_TAX_ANNUAL_THRESHOLD_AUD
    rate = QLD_PAYROLL_TAX_RATE
    over = max(0.0, wages - threshold)
    liab = round(over * rate * 100) / 100
    severity = "warning" if wages > threshold else "info"
    title = (
        f"[{entity_code}] Wages above QLD payroll tax threshold"
        if severity == "warning"
        else f"[{entity_code}] QLD payroll tax position: ${liab:.2f}"
    )
    detail_lines = [
        f"{entity_code} 12-month wages: ${wages:.2f}.",
        f"QLD threshold: ${threshold:.2f} @ {rate * 100:.2f}%.",
        f"Estimated payroll tax: ${liab:.2f}.",
    ]
    if paid_ytd is not None:
        delta = liab - paid_ytd
        if abs(delta) < 1.0:
            position = "looks current — paid roughly matches estimated liability."
        elif delta > 0:
            position = f"GAP: estimated $${delta:.0f} more owing than paid (could be normal timing — confirm with QRO portal)."
        else:
            position = f"OVER: paid $${-delta:.0f} more than estimated (likely true; estimate is rolling 12mo, paid is YTD expense)."
        detail_lines.append(f"Paid YTD (P&L expense account): ${paid_ytd:.2f}. {position}")
        if paid_components:
            detail_lines.append(
                "Paid components: "
                + ", ".join(f"{k} ${v:.2f}" for k, v in paid_components.items())
            )
    if components:
        detail_lines.append(
            "Wages components: "
            + ", ".join(f"{k} ${v:.2f}" for k, v in components.items())
        )
    detail_lines.append(
        "GROUPED calc — TAX_PAYROLL_TAX_GROUPED=true."
        if entity_code == "GROUPED"
        else "Per-entity calc. Set TAX_PAYROLL_TAX_GROUPED=true if SC+CQ "
             "are grouped for QLD payroll tax."
    )
    return {
        "detector": "payroll-tax-threshold",
        "domain": "payroll-tax",
        "severity": severity,
        "entity_code": entity_code,
        "title": title,
        "detail": "\n".join(detail_lines),
        "amount": liab,
        "evidence": {
            "dedupKey": f"payroll-tax-threshold:{entity_code}:{period_label}",
            "entityCode": entity_code,
            "wages12mo": wages,
            "threshold": threshold,
            "rate": rate,
            "liability": liab,
            "grouped": entity_code == "GROUPED",
            "components": components or {},
            "paidYtd": paid_ytd,
            "paidComponents": paid_components or {},
            **ruleset_meta(),
        },
    }


def run_payroll_tax() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    period_label = today_bne().isoformat()[:7]  # YYYY-MM for dedupKey stability

    per_ent: dict[str, float] = {}
    paid_per_ent: dict[str, float] = {}
    for entity in ("SC", "CQ"):
        if not xero_tax.tenant_configured(entity):
            continue
        wages, codes = _wages_for(entity)
        if wages is None:
            # Gap warning — XERO_<E>_WAGES_ACCOUNT_CODES unset or P&L unreadable.
            out.append({
                "detector": "payroll-tax-threshold",
                "domain": "payroll-tax",
                "severity": "warning",
                "entity_code": entity,
                "title": f"[{entity}] Payroll-tax wages source not configured",
                "detail": (
                    f"{entity}: XERO_{entity}_WAGES_ACCOUNT_CODES is not set "
                    f"or Xero P&L returned no usable balance. Set it to the "
                    f"comma-separated gross-wages account codes."
                ),
                "evidence": {
                    "dedupKey": f"payroll-tax-threshold:{entity}:unmapped",
                    "entityCode": entity,
                    "codesTried": codes,
                    **ruleset_meta(),
                },
            })
            continue
        per_ent[entity] = wages
        paid, _paid_codes = _payroll_tax_paid_ytd(entity)
        if paid is not None:
            paid_per_ent[entity] = paid

    if _grouped() and len(per_ent) == 2:
        total = sum(per_ent.values())
        total_paid = sum(paid_per_ent.values()) if paid_per_ent else None
        out.append(_emit(
            "GROUPED", total,
            period_label=period_label,
            components={k: v for k, v in per_ent.items()},
            paid_ytd=total_paid,
            paid_components={k: v for k, v in paid_per_ent.items()} if paid_per_ent else None,
        ))
    else:
        for entity, wages in per_ent.items():
            out.append(_emit(
                entity, wages,
                period_label=period_label,
                paid_ytd=paid_per_ent.get(entity),
            ))

    return out
