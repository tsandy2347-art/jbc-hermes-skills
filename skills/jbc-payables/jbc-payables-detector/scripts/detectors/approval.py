"""Group C — approval-pending detector.

Emits:
  approval-pending (info) — ACCPAY bill in DRAFT or SUBMITTED status older
                            than PAYABLES_APPROVAL_PENDING_DAYS. Surfaces
                            to Mark so pending drafts don't go stale.
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


def run_approval(entity: str, *, lookback_days: int) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    threshold_days = _env_int("PAYABLES_APPROVAL_PENDING_DAYS", 2)
    since_iso = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
    bills = list_accpay_invoices(entity, since_iso=since_iso)

    now = _dt.datetime.now(_dt.timezone.utc)
    for inv in bills:
        status = str(inv.get("Status") or "").upper()
        if status not in ("DRAFT", "SUBMITTED"):
            continue
        d = parse_xero_date(inv.get("Date")) or parse_xero_date(inv.get("UpdatedDateUTC"))
        if d is None:
            continue
        age_days = (now - d).total_seconds() / 86_400.0
        if age_days < threshold_days:
            continue
        inv_id = inv.get("InvoiceID", "")
        number = inv.get("InvoiceNumber") or "(no number)"
        contact = (inv.get("Contact") or {}).get("Name") or "(unknown supplier)"
        total = float(inv.get("Total") or 0)
        findings.append({
            "detector": "approval-pending",
            "domain": "ap",
            "severity": "info",
            "entity_code": entity,
            "title": f"{status} bill awaiting approval ({age_days:.0f}d) — {contact} {number}",
            "detail": (
                f"Bill is in {status} state, dated {d.date().isoformat()} "
                f"({age_days:.0f} days ago). Beyond the "
                f"{threshold_days}-day staleness threshold — nudge approver or "
                f"close the draft."
            ),
            "amount": total if total else None,
            "evidence": {
                "dedupKey": f"approval-pending:{entity}:{inv_id}",
                "kind": "approval-pending",
                "xeroInvoiceId": inv_id,
                "invoiceNumber": number,
                "supplierName": contact,
                "status": status,
                "ageDays": round(age_days, 1),
            },
        })
    return findings
