"""Group D — payment-run-proposed detector.

Emits one info finding per entity per day summarising the AUTHORISED,
unpaid, due-within-lead-time bills. Informational only — this skill
does NOT batch, release, or pay anything. A human releases via Xero
directly.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from ..xero_client import list_accpay_invoices, parse_xero_date


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def run_payment_run(entity: str, *, lookback_days: int) -> list[dict[str, Any]]:
    lead_days = _env_int("PAYABLES_PAYMENT_LEAD_DAYS", 3)
    since_iso = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
    bills = list_accpay_invoices(entity, since_iso=since_iso)

    horizon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=lead_days)
    eligible: list[dict[str, Any]] = []
    total = 0.0
    for inv in bills:
        status = str(inv.get("Status") or "").upper()
        if status != "AUTHORISED":
            continue
        try:
            amt_due = float(inv.get("AmountDue") or 0)
        except (TypeError, ValueError):
            amt_due = 0.0
        if amt_due <= 0.0:
            continue
        due = parse_xero_date(inv.get("DueDate"))
        if due is not None and due > horizon:
            continue
        eligible.append({
            "xeroInvoiceId": inv.get("InvoiceID", ""),
            "invoiceNumber": inv.get("InvoiceNumber") or None,
            "supplierName": (inv.get("Contact") or {}).get("Name"),
            "amountDue": amt_due,
            "dueDate": due.date().isoformat() if due else None,
        })
        total += amt_due

    if not eligible:
        return []

    today_iso = _dt.date.today().isoformat()
    return [{
        "detector": "payment-run-proposed",
        "domain": "ap",
        "severity": "info",
        "entity_code": entity,
        "title": (
            f"Payment run candidate — {entity}: {len(eligible)} bills, "
            f"A${total:,.2f}"
        ),
        "detail": (
            f"{len(eligible)} AUTHORISED, unpaid {entity} ACCPAY bill(s) totalling "
            f"A${total:,.2f} are due within the next {lead_days} day(s). This is an "
            f"informational candidate list — release via Xero directly. The detector "
            f"does not batch or mutate Xero state."
        ),
        "amount": round(total, 2),
        "evidence": {
            "dedupKey": f"payment-run-proposed:{entity}",
            "kind": "payment-run-proposed",
            "entityCode": entity,
            "leadDays": lead_days,
            "invoiceCount": len(eligible),
            "bills": eligible[:200],  # cap evidence size
        },
    }]
