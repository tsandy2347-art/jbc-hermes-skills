"""Cash-application detectors.

unallocated-receipt (warning) — ACCRECPAYMENT with no link to a known
                                outstanding invoice, older than the
                                grace window.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

from ..xero_ar import masked_ref, parse_xero_date


def _setting_int(key: str, env_name: str, default: int) -> int:
    raw = os.environ.get("_AR_SETTINGS_JSON")
    if raw:
        try:
            s = json.loads(raw)
            if key in s:
                return int(s[key])
        except Exception:
            pass
    raw_env = os.environ.get(env_name)
    if raw_env:
        try:
            return int(raw_env)
        except ValueError:
            pass
    return default


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
    """A receipt is unallocated when it has NO linked invoice at all (the
    payment was recorded without an Invoice.InvoiceID at the time). A payment
    whose invoice has since been fully paid is NOT unallocated — that's the
    normal happy-path lifecycle.

    Gated on age >= `AR_UNALLOCATED_RECEIPT_AGE_DAYS` (default 2) and
    deduped per PaymentID so a payment that appears more than once in
    Xero's paginated response (which it does) only emits one finding.
    """
    grace_days = _setting_int("unallocated_receipt_age_days", "AR_UNALLOCATED_RECEIPT_AGE_DAYS", 2)
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in payments:
        payment_id = p.get("PaymentID") or ""
        if payment_id in seen:
            continue
        seen.add(payment_id)
        invoice = p.get("Invoice") or {}
        linked_id = invoice.get("InvoiceID")
        # Truly unallocated = no linked invoice at all. Skip anything that
        # was linked, even if the linked invoice is now closed.
        if linked_id:
            continue
        rec = parse_xero_date(p.get("Date"))
        if not rec:
            continue
        age_days = (now - rec).days
        if age_days < grace_days:
            continue
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
                f"Receipt of {_fmt_aud(amount)} dated {date_iso} has no linked "
                f"ACCREC invoice in {entity} ({age_days} days old, grace {grace_days}). "
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
