"""QLD payroll-tax detector — Domain C.

Pulls a 12-month wages position per entity from Xero's Profit & Loss
report (sum of wages-account balances over the last 365 days) and the
payroll-tax expense booked against it.

WHAT IT ALERTS ON (rewritten Oct 2026)
  The old version raised "Wages above QLD payroll tax threshold" every
  month, because ~$13m of wages is always above $1.3m, and put its own
  liability estimate in `amount`. Mark promotes any amount over $25k to
  "needs you today", and the narrative then read the estimate as a wages
  figure — so a group that had paid MORE than the estimate headlined as
  an amber payroll-tax emergency (5 Oct 2026 brief).

  Being over the threshold is not news. What would be news is payroll tax
  not being paid, so the warning conditions are:
    1. no payroll-tax expense booked for the last month that should have
       one (a missed monthly return), or
    2. the effective rate paid over 12 months (paid / wages) falling below
       TAX_PAYROLL_TAX_MIN_EFFECTIVE_RATE (default 3.5%; JBC runs ~4.7%).
  Otherwise the finding is info with no amount.

  The statutory estimate stays in the detail as context only. It applies
  the tiered rate and deduction phase-out, but not the mental health levy,
  the regional discount (CQ may qualify) or super-in-taxable-wages, any of
  which moves it by tens of thousands — too loose to alarm on.

GROUPING:
  - Default: per-entity comparison.
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
    QLD_PAYROLL_TAX_HIGH_RATE,
    QLD_PAYROLL_TAX_HIGH_RATE_FROM_AUD,
    QLD_PAYROLL_TAX_PHASEOUT_FROM_AUD,
    QLD_PAYROLL_TAX_RATE,
    ruleset_meta,
)
from scripts.periods import today_bne

DEFAULT_MIN_EFFECTIVE_RATE = 0.035


def _grouped() -> bool:
    return os.environ.get("TAX_PAYROLL_TAX_GROUPED", "").lower() == "true"


def _min_effective_rate() -> float:
    raw = os.environ.get("TAX_PAYROLL_TAX_MIN_EFFECTIVE_RATE", "")
    try:
        return float(raw) if raw else DEFAULT_MIN_EFFECTIVE_RATE
    except ValueError:
        return DEFAULT_MIN_EFFECTIVE_RATE


def _sum_accounts(entity: str, env_var: str, from_iso: str,
                  to_iso: str) -> tuple[float | None, list[str]]:
    """Return (P&L total across the account codes in `env_var`, codes tried)."""
    codes = xero_tax.split_codes(os.environ.get(env_var))
    if not codes:
        return None, []
    pl = xero_tax.profit_and_loss(entity, from_iso, to_iso)
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


def _last_year() -> tuple[str, str]:
    today = today_bne()
    return (today - _dt.timedelta(days=365)).isoformat(), today.isoformat()


def _wages_for(entity: str) -> tuple[float | None, list[str]]:
    """12-month wages total for an entity, or None if unmapped/unreadable."""
    return _sum_accounts(entity, f"XERO_{entity}_WAGES_ACCOUNT_CODES", *_last_year())


def _payroll_tax_paid_ytd(entity: str) -> tuple[float | None, list[str]]:
    """12-month payroll-tax expense. Whichever bookkeeping pattern is used
    (direct-to-expense or via a liability clearing account) the EXPENSE
    account ends up with the cost, so this is "paid + accrued" — the closest
    thing to what the QRO has been paid without going to the QRO portal.
    """
    return _sum_accounts(
        entity, f"XERO_{entity}_PAYROLL_TAX_EXPENSE_ACCOUNT_CODES", *_last_year()
    )


def _check_month(today: _dt.date) -> tuple[_dt.date, _dt.date]:
    """The latest month that should already carry a payroll-tax expense.

    The monthly return is due on the 7th, and the accrual is often booked
    around then, so until the 10th we look at the month before last.
    """
    first_this = today.replace(day=1)
    end = first_this - _dt.timedelta(days=1)
    if today.day <= 10:
        end = end.replace(day=1) - _dt.timedelta(days=1)
    return end.replace(day=1), end


def _payroll_tax_for_month(entity: str, start: _dt.date,
                           end: _dt.date) -> float | None:
    total, _codes = _sum_accounts(
        entity, f"XERO_{entity}_PAYROLL_TAX_EXPENSE_ACCOUNT_CODES",
        start.isoformat(), end.isoformat(),
    )
    return total


def estimate_liability(wages: float) -> float:
    """Approximate annual QLD payroll tax: tiered rate + deduction phase-out.

    Excludes the mental health levy, the regional discount and any super
    that belongs in taxable wages — context only, never an alarm.
    """
    deduction = QLD_PAYROLL_TAX_ANNUAL_THRESHOLD_AUD
    if wages > QLD_PAYROLL_TAX_PHASEOUT_FROM_AUD:
        deduction = max(0.0, deduction - (wages - QLD_PAYROLL_TAX_PHASEOUT_FROM_AUD) / 4)
    rate = (QLD_PAYROLL_TAX_HIGH_RATE if wages > QLD_PAYROLL_TAX_HIGH_RATE_FROM_AUD
            else QLD_PAYROLL_TAX_RATE)
    return round(max(0.0, wages - deduction) * rate * 100) / 100


def _money(n: float) -> str:
    return f"${n:,.0f}"


def _emit(entity_code: str, wages: float, *, period_label: str,
          components: dict[str, float] | None = None,
          paid_12mo: float | None = None,
          paid_components: dict[str, float] | None = None,
          month: tuple[_dt.date, _dt.date] | None = None,
          month_paid: float | None = None,
          month_components: dict[str, float] | None = None) -> dict[str, Any]:
    threshold = QLD_PAYROLL_TAX_ANNUAL_THRESHOLD_AUD
    over_threshold = wages > threshold
    estimate = estimate_liability(wages)
    min_rate = _min_effective_rate()
    effective = (paid_12mo / wages) if (paid_12mo is not None and wages) else None
    month_label = month[0].strftime("%b %Y") if month else None

    problems: list[str] = []
    # Per entity when grouped: a group total would hide one member's missing
    # month behind the other's payment.
    missing = sorted(k for k, v in (month_components or {}).items() if v <= 0)
    if not month_components and month_paid is not None and month_paid <= 0:
        missing = [entity_code]
    if over_threshold and month and missing:
        problems.append(
            f"no payroll-tax expense booked for {month_label} "
            f"({', '.join(missing)}) — the monthly return may not have been lodged"
        )
    if over_threshold and effective is not None and effective < min_rate:
        problems.append(
            f"effective rate paid over 12 months is {effective * 100:.2f}%, "
            f"below the {min_rate * 100:.1f}% floor"
        )

    if problems:
        severity = "warning"
        title = f"[{entity_code}] Payroll tax may be behind — " + "; ".join(problems)
        # Size of the possible gap, not of the whole liability, so Mark ranks
        # it by what is actually at stake: the shortfall against the floor,
        # plus a typical month for each entity with nothing booked.
        gap = max(0.0, wages * min_rate - paid_12mo) if paid_12mo is not None else 0.0
        per_entity_paid = paid_components or (
            {entity_code: paid_12mo} if paid_12mo is not None else {})
        gap += sum(per_entity_paid.get(k, 0.0) / 12 for k in missing)
        amount = round(gap * 100) / 100 if gap else None
    elif over_threshold and paid_12mo is None:
        severity = "info"
        title = (f"[{entity_code}] Payroll tax not verified — expense account "
                 f"codes unmapped")
        amount = None
    elif over_threshold:
        severity = "info"
        title = (
            f"[{entity_code}] Payroll tax looks current — {_money(paid_12mo)} "
            f"paid on {_money(wages)} wages over 12 months "
            f"({effective * 100:.1f}%)"
        )
        amount = None
    else:
        severity = "info"
        title = f"[{entity_code}] Wages {_money(wages)} under the QLD payroll tax threshold"
        amount = None

    detail_lines = [
        f"{entity_code} 12-month wages: {_money(wages)}.",
    ]
    if paid_12mo is not None:
        detail_lines.append(
            f"Payroll tax expensed over 12 months: {_money(paid_12mo)} "
            f"(effective rate {effective * 100:.2f}%; alert floor "
            f"{min_rate * 100:.1f}%)."
        )
    else:
        detail_lines.append(
            f"Payroll tax paid: unknown — set XERO_<E>_PAYROLL_TAX_EXPENSE_ACCOUNT_CODES."
        )
    if month_label:
        detail_lines.append(
            f"Expense booked for {month_label}: "
            + (_money(month_paid) if month_paid is not None else "unknown")
            + (" (" + ", ".join(f"{k} {_money(v)}" for k, v in month_components.items()) + ")"
               if month_components else "")
            + "."
        )
    detail_lines.append(
        f"Rough statutory estimate: {_money(estimate)} (tiered rate + phase-out "
        f"only; excludes mental health levy, regional discount and super-in-wages, "
        f"so it is context, not a target)."
    )
    if paid_components:
        detail_lines.append(
            "Paid components: "
            + ", ".join(f"{k} {_money(v)}" for k, v in paid_components.items())
        )
    if components:
        detail_lines.append(
            "Wages components: "
            + ", ".join(f"{k} {_money(v)}" for k, v in components.items())
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
        "amount": amount,
        "evidence": {
            "dedupKey": f"payroll-tax-threshold:{entity_code}:{period_label}",
            "entityCode": entity_code,
            "wages12mo": wages,
            "threshold": threshold,
            "estimate": estimate,
            "paid12mo": paid_12mo,
            "paidYtd": paid_12mo,  # old name, kept for existing readers
            "effectiveRate": effective,
            "minEffectiveRate": min_rate,
            "checkMonth": month[0].isoformat() if month else None,
            "checkMonthPaid": month_paid,
            "checkMonthComponents": month_components or {},
            "problems": problems,
            "grouped": entity_code == "GROUPED",
            "components": components or {},
            "paidComponents": paid_components or {},
            **ruleset_meta(),
        },
    }


def run_payroll_tax() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    today = today_bne()
    period_label = today.isoformat()[:7]  # YYYY-MM for dedupKey stability
    month = _check_month(today)

    per_ent: dict[str, float] = {}
    paid_per_ent: dict[str, float] = {}
    month_per_ent: dict[str, float] = {}
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
        m = _payroll_tax_for_month(entity, *month)
        if m is not None:
            month_per_ent[entity] = m

    if _grouped() and len(per_ent) == 2:
        out.append(_emit(
            "GROUPED", sum(per_ent.values()),
            period_label=period_label,
            components=dict(per_ent),
            paid_12mo=sum(paid_per_ent.values()) if len(paid_per_ent) == 2 else None,
            paid_components=dict(paid_per_ent) or None,
            month=month,
            month_paid=sum(month_per_ent.values()) if len(month_per_ent) == 2 else None,
            month_components=dict(month_per_ent) or None,
        ))
    else:
        for entity, wages in per_ent.items():
            out.append(_emit(
                entity, wages,
                period_label=period_label,
                paid_12mo=paid_per_ent.get(entity),
                month=month,
                month_paid=month_per_ent.get(entity),
            ))

    return out
