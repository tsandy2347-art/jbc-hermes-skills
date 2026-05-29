"""GST detectors — Domain A.

Three detectors, all per-entity (never consolidated):
  - gst-position         (info, live net GST for the open BAS period)
  - gst-coding-anomaly   (warning, untagged net or implied-rate variance)
  - gst-cash-shortfall   (warning <80% coverage, critical <50%)
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from scripts import xero_tax
from scripts.jbc_tax_rulesets import (
    ATO_GST_RATE,
    DEFAULT_GST_CASH_CRITICAL_PCT,
    DEFAULT_GST_CASH_WARNING_PCT,
    DEFAULT_UNTAGGED_NET_THRESHOLD_AUD,
    ruleset_meta,
)
from scripts.periods import bas_period_for, today_bne

SALES_TAX_TYPES = {"OUTPUT", "OUTPUT2", "OUTPUTSERVICES"}
PURCHASE_TAX_TYPES = {"INPUT", "INPUT2", "INPUTSERVICES"}
GST_FREE_INCOME = {"EXEMPTOUTPUT", "ZERORATEDOUTPUT", "GSTFREEINCOME"}
GST_FREE_EXPENSE = {"EXEMPTEXPENSES", "ZERORATEDEXPENSES", "GSTFREEEXPENSES"}
UNTAGGED = {"NONE", "UNTAGGED"}


def _round2(n: float) -> float:
    return round(n * 100) / 100


def _bas_cycle(entity: str) -> str:
    return os.environ.get(f"TAX_BAS_CYCLE_{entity}", "quarterly")


def _gst_account_codes(entity: str, accounts: list[dict[str, Any]]) -> list[str]:
    raw = os.environ.get(f"XERO_{entity}_GST_ACCOUNT_CODES")
    codes = xero_tax.split_codes(raw)
    if codes:
        return codes
    # Fall back to Xero SystemAccount=="GST" control account.
    return [a.get("Code") for a in accounts
            if a.get("SystemAccount") == "GST" and a.get("Code")]


def _compute_gst_aggregates(tax_types: dict[str, dict[str, Any]]) -> dict[str, float]:
    on_sales = on_purch = gst_free_inc = gst_free_exp = untagged_net = 0.0
    for agg in tax_types.values():
        if agg["taxType"] in SALES_TAX_TYPES:
            on_sales += agg["tax"]
        elif agg["taxType"] in PURCHASE_TAX_TYPES:
            on_purch += agg["tax"]
        elif agg["taxType"] in GST_FREE_INCOME:
            gst_free_inc += agg["net"]
        elif agg["taxType"] in GST_FREE_EXPENSE:
            gst_free_exp += agg["net"]
        elif agg["taxType"] in UNTAGGED:
            untagged_net += agg["net"]
    return {
        "gstOnSales": _round2(on_sales),
        "gstOnPurchases": _round2(on_purch),
        "netGst": _round2(on_sales - on_purch),
        "gstFreeIncome": _round2(gst_free_inc),
        "gstFreeExpenses": _round2(gst_free_exp),
        "untaggedNet": _round2(untagged_net),
    }


def _derive_cash_set_aside(report: dict[str, Any] | None,
                           accounts: list[dict[str, Any]],
                           codes: list[str]) -> float | None:
    if not report or not codes:
        return None
    total = 0.0
    found = 0
    for c in codes:
        acc = next((a for a in accounts if a.get("Code") == c), None)
        name = acc.get("Name") if acc else None
        bal = xero_tax.find_account_balance(report, c, name)
        if bal is None:
            continue
        found += 1
        total += abs(bal)
    return _round2(total) if found else None


def run_gst(entity: str, *, lookback_days: int) -> list[dict[str, Any]]:
    """Pull a per-entity Xero snapshot and emit all GST findings."""
    if not xero_tax.tenant_configured(entity):
        return []

    out: list[dict[str, Any]] = []
    today = today_bne()
    since = _dt.datetime.combine(
        today - _dt.timedelta(days=lookback_days), _dt.time.min,
        tzinfo=_dt.timezone.utc,
    )

    accounts = xero_tax.list_accounts(entity)
    journals = xero_tax.list_journals_since(entity, since)
    tax_types = xero_tax.aggregate_tax_types(journals)
    gst = _compute_gst_aggregates(tax_types)

    period = bas_period_for(today, _bas_cycle(entity))
    tb = xero_tax.trial_balance(entity, today.isoformat())
    codes = _gst_account_codes(entity, accounts)
    cash = _derive_cash_set_aside(tb, accounts, codes)

    meta = ruleset_meta()

    # 1) gst-position (info)
    out.append({
        "detector": "gst-position",
        "domain": "gst",
        "severity": "info",
        "entity_code": entity,
        "title": f"[{entity}] Live GST position {period.label}: ${gst['netGst']:.2f} owed",
        "detail": (
            f"{entity} live GST for {period.label}:\n"
            f"  GST on sales:     ${gst['gstOnSales']:.2f}\n"
            f"  GST on purchases: ${gst['gstOnPurchases']:.2f}\n"
            f"  Net GST owed:     ${gst['netGst']:.2f}\n"
            f"  Cash set aside:   "
            + ("(not tracked — set XERO_*_GST_ACCOUNT_CODES)" if cash is None
               else f"${cash:.2f}")
            + "\n"
            f"  GST-free income:  ${gst['gstFreeIncome']:.2f}\n"
            f"  Untagged net:     ${gst['untaggedNet']:.2f} (needs review)\n"
            f"  Period: {period.start.isoformat()} → {period.end.isoformat()}\n"
            f"  Due:    {period.due_date.isoformat()}"
        ),
        "amount": gst["netGst"],
        "evidence": {
            "dedupKey": f"gst-position:{entity}:{period.label}",
            "entityCode": entity,
            "period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
                "label": period.label,
                "dueIso": period.due_date.isoformat(),
            },
            "gst": gst,
            "cashSetAside": cash,
            "lookbackDays": lookback_days,
            **meta,
        },
    })

    # 2) gst-coding-anomaly (warning)
    threshold = float(os.environ.get(
        "TAX_UNTAGGED_NET_THRESHOLD_AUD",
        DEFAULT_UNTAGGED_NET_THRESHOLD_AUD,
    ))
    for agg in tax_types.values():
        tt = agg["taxType"]
        net = agg["net"]
        tax = agg["tax"]
        # Untagged lines with material net.
        if tt in UNTAGGED and abs(net) >= threshold:
            out.append({
                "detector": "gst-coding-anomaly",
                "domain": "gst",
                "severity": "warning",
                "entity_code": entity,
                "title": f"[{entity}] ${abs(net):.2f} in untagged GST lines",
                "detail": (
                    f"{entity} has {agg['lineCount']} line(s) tagged \"{tt}\" "
                    f"with net ${net:.2f} in the {lookback_days}d lookback. "
                    f"Untagged GST risks misstated G1/G11/1A/1B BAS labels — "
                    f"have the external accountant re-tag or confirm."
                ),
                "amount": _round2(abs(net)),
                "evidence": {
                    "dedupKey": f"gst-coding-anomaly:{entity}:{tt}",
                    "entityCode": entity,
                    "kind": "untagged",
                    "taxType": tt,
                    "net": net,
                    "lineCount": agg["lineCount"],
                    **meta,
                },
            })
            continue
        # OUTPUT lines whose implied rate ≠ statutory GST rate.
        if tt == "OUTPUT" and abs(net) >= threshold and net != 0:
            implied = tax / net
            if abs(implied - ATO_GST_RATE) > 0.005:
                out.append({
                    "detector": "gst-coding-anomaly",
                    "domain": "gst",
                    "severity": "warning",
                    "entity_code": entity,
                    "title": (
                        f"[{entity}] OUTPUT lines imply GST rate "
                        f"{implied * 100:.2f}% (expected {ATO_GST_RATE * 100:.2f}%)"
                    ),
                    "detail": (
                        f"{entity} has {agg['lineCount']} OUTPUT line(s) "
                        f"totalling net ${net:.2f} / tax ${tax:.2f}, implying a "
                        f"{implied * 100:.2f}% rate vs the ATO {ATO_GST_RATE * 100:.2f}%. "
                        f"Either some lines have a non-standard tax code or the "
                        f"tax amount is mis-recorded. Confirm with the external accountant."
                    ),
                    "amount": _round2(net),
                    "evidence": {
                        "dedupKey": f"gst-coding-anomaly:{entity}:OUTPUT-rate",
                        "entityCode": entity,
                        "kind": "implied-rate-variance",
                        "taxType": tt,
                        "net": net,
                        "tax": tax,
                        "impliedRate": implied,
                        "expectedRate": ATO_GST_RATE,
                        **meta,
                    },
                })

    # 3) gst-cash-shortfall — only when net GST > 0.
    if gst["netGst"] > 0:
        if cash is None:
            out.append({
                "detector": "gst-cash-shortfall",
                "domain": "gst",
                "severity": "warning",
                "entity_code": entity,
                "title": f"[{entity}] GST cash-set-aside not tracked",
                "detail": (
                    f"{entity} has ${gst['netGst']:.2f} net GST owed for "
                    f"{period.label} but the GST liability/clearing account codes "
                    f"are not mapped. Set XERO_{entity}_GST_ACCOUNT_CODES so the "
                    f"skill can compare cash to liability."
                ),
                "amount": gst["netGst"],
                "evidence": {
                    "dedupKey": f"gst-cash-shortfall:{entity}:{period.label}",
                    "entityCode": entity,
                    "kind": "untracked",
                    "netGst": gst["netGst"],
                    "cashSetAside": None,
                    **meta,
                },
            })
        else:
            coverage = cash / gst["netGst"]
            warn = float(os.environ.get(
                "TAX_GST_CASH_WARNING_PCT", DEFAULT_GST_CASH_WARNING_PCT,
            ))
            crit = float(os.environ.get(
                "TAX_GST_CASH_CRITICAL_PCT", DEFAULT_GST_CASH_CRITICAL_PCT,
            ))
            if coverage < warn:
                severity = "critical" if coverage < crit else "warning"
                gap = _round2(gst["netGst"] - cash)
                out.append({
                    "detector": "gst-cash-shortfall",
                    "domain": "gst",
                    "severity": severity,
                    "entity_code": entity,
                    "title": (
                        f"[{entity}] GST cash gap ${gap:.2f} for {period.label}"
                    ),
                    "detail": (
                        f"{entity} owes ${gst['netGst']:.2f} net GST for "
                        f"{period.label}. Cash set aside in mapped accounts: "
                        f"${cash:.2f} ({coverage * 100:.0f}% coverage). "
                        f"Gap: ${gap:.2f}. Due {period.due_date.isoformat()}. "
                        f"Move funds to the GST clearing balance before due date."
                    ),
                    "amount": gap,
                    "evidence": {
                        "dedupKey": f"gst-cash-shortfall:{entity}:{period.label}",
                        "entityCode": entity,
                        "kind": "coverage-shortfall",
                        "netGst": gst["netGst"],
                        "cashSetAside": cash,
                        "coverage": coverage,
                        "gap": gap,
                        "warnPct": warn,
                        "critPct": crit,
                        **meta,
                    },
                })

    return out
