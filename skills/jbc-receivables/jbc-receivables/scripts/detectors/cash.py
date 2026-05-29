"""Cash-application detectors.

unallocated-receipt (warning) — ACCRECPAYMENT with no link to a known
                                outstanding invoice, older than the
                                grace window.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from ..xero_ar import masked_ref, parse_xero_date


def _fmt_aud(n: float) -> str:
    return f"A${n:,.2f}"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def run_unallocated_receipts(
    entity: str,
    payments: list[dict[str, Any]],
    open_invoice_ids: set[str],
    *,
    now: _dt.datetime,
) -> list[dict[str, Any]]:
    """A receipt is unallocated when it lacks an Invoice.InvoiceID link or
    its linked invoice isn't in the open set, AND it's older than
    `AR_UNALLOCATED_RECEIPT_AGE_DAYS` (default 2)."""
    grace_days = _env_int("AR_UNALLOCATED_RECEIPT_AGE_DAYS", 2)
    findings: list[dict[str, Any]] = []
    for p in payments:
        invoice = p.get("Invoice") or {}
        linked_id = invoice.get("InvoiceID")
        if linked_id and linked_id in open_invoice_ids:
            continue  # matched fine
        # Fully-paid invoices have left the open pool. We treat these as
        # unallocated only if also old — old payments with no open invoice
        # behind them are the suspicious case.
        rec = parse_xero_date(p.get("Date"))
        if not rec:
            continue
        age_days = (now - rec).days
        if age_days < grace_days:
            continue

        payment_id = p.get("PaymentID") or ""
        contact_name = (invoice.get("Contact") or {}).get("Name")
        contact_id = (invoice.get("Contact") or {}).get("ContactID") or payment_id
        ref = masked_ref(contact_name, contact_id) if contact_name else (
            f"pay-{payment_id[-6:]}" if payment_id else "pay-?"
        )
        amount = float(p.get("Amount") or 0)
        date_iso = (p.get("Date") or "")[:10]
        findings.append({
            "detector": "unallocated-receipt",
            "domain": "ar",
            "severity": "warning",
            "entity_code": entity,
            "title": f"{entity}: unallocated receipt {ref} — {_fmt_aud(amount)}",
            "detail": (
                f"Receipt of {_fmt_aud(amount)} dated {date_iso} has no link to a "
                f"currently outstanding ACCREC invoice in {entity} "
                f"({age_days} days old, grace {grace_days}). "
                f"Money is in but uncoded. Match to the right invoice "
                f"or refund as appropriate."
            ),
            "amount": amount,
            "evidence": {
                "dedupKey": f"unallocated-receipt:{entity}:{payment_id}",
                "kind": "unallocated-receipt",
                "xeroPaymentId": payment_id,
                "linkedInvoiceId": linked_id,
                "receivedDate": p.get("Date"),
                "ageDays": age_days,
                "graceDays": grace_days,
                "contactRef": ref,
                "reference": p.get("Reference"),
            },
        })
    return findings
