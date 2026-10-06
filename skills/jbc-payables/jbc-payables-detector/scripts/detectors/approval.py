"""Group C — approval-pending detector.

Emits:
  approval-pending (info; warning if any > PAYABLES_APPROVAL_STALE_DAYS) —
                            ONE finding per entity summarising ACCPAY bills
                            in DRAFT/SUBMITTED older than
                            PAYABLES_APPROVAL_PENDING_DAYS.
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
    stale_days = _env_int("PAYABLES_APPROVAL_STALE_DAYS", 14)
    pending: list[dict[str, Any]] = []
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
        pending.append({
            "xeroInvoiceId": inv.get("InvoiceID", ""),
            "invoiceNumber": inv.get("InvoiceNumber") or "(no number)",
            "supplierName": (inv.get("Contact") or {}).get("Name") or "(unknown supplier)",
            "status": status,
            "date": d.date().isoformat(),
            "ageDays": round(age_days, 1),
            "total": float(inv.get("Total") or 0),
        })
    if not pending:
        return findings

    # ONE finding per entity. Per-bill rows were invisible until Oct 2026
    # (the bill fetch stopped at 5,000 rows); complete, SC alone had ~200
    # drafts, which would have been ~200 brief lines. The total is in the
    # title, not `amount`, so a large but routine draft queue does not jump
    # to "needs you today".
    pending.sort(key=lambda r: -r["ageDays"])
    total = round(sum(r["total"] for r in pending), 2)
    stale = [r for r in pending if r["ageDays"] >= stale_days]
    by_supplier: dict[str, float] = {}
    for r in pending:
        by_supplier[r["supplierName"]] = by_supplier.get(r["supplierName"], 0.0) + r["total"]
    top = sorted(by_supplier.items(), key=lambda kv: -kv[1])[:5]
    oldest = pending[0]
    findings.append({
        "detector": "approval-pending",
        "domain": "ap",
        "severity": "warning" if stale else "info",
        "entity_code": entity,
        "title": (
            f"{entity}: {len(pending)} draft bill(s) awaiting approval "
            f"(${total:,.0f})"
            + (f" — {len(stale)} older than {stale_days} days" if stale else "")
        ),
        "detail": (
            f"{len(pending)} bill(s) in DRAFT/SUBMITTED for more than "
            f"{threshold_days} days, totalling ${total:,.2f}. Oldest: "
            f"{oldest['supplierName']} {oldest['invoiceNumber']} "
            f"({oldest['ageDays']:.0f} days). Largest by supplier: "
            + ", ".join(f"{n} ${v:,.0f}" for n, v in top)
            + ". Approve or close them in Xero."
        ),
        "amount": None,
        "evidence": {
            "dedupKey": f"approval-pending:{entity}",
            "kind": "approval-pending",
            "billCount": len(pending),
            "staleCount": len(stale),
            "total": total,
            "bills": pending[:100],
        },
    })
    return findings
