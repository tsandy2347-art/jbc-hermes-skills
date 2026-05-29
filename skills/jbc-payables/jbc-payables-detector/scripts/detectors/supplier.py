"""Group B — supplier-scoped detectors.

Emits:
  no-abn                    (warning)  — supplier has open ACCPAY bills but no TaxNumber
  invalid-abn               (warning)  — supplier TaxNumber fails ATO checksum
  new-supplier-quarantine   (critical) — supplier Contact UpdatedDateUTC inside window
                                         AND has at least one ACCPAY bill in window
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from ..abn import is_valid_abn, normalise_abn
from ..xero_client import list_accpay_invoices, list_supplier_contacts, parse_xero_date


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def run_supplier(entity: str, *, lookback_days: int) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    new_window = _env_int("PAYABLES_NEW_SUPPLIER_WINDOW_DAYS", 30)
    since_iso = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()

    contacts = list_supplier_contacts(entity)
    bills = list_accpay_invoices(entity, since_iso=since_iso)

    # Build supplier_id -> (count, total, sample_bill)
    by_supplier: dict[str, dict[str, Any]] = {}
    for inv in bills:
        cid = (inv.get("Contact") or {}).get("ContactID")
        if not cid:
            continue
        b = by_supplier.setdefault(cid, {"count": 0, "total": 0.0, "sample": inv})
        b["count"] += 1
        try:
            b["total"] += float(inv.get("Total") or 0)
        except (TypeError, ValueError):
            pass

    now = _dt.datetime.now(_dt.timezone.utc)

    for c in contacts:
        cid = c.get("ContactID", "")
        name = c.get("Name") or "(unnamed supplier)"
        abn_raw = c.get("TaxNumber") or None
        bucket = by_supplier.get(cid)

        # ABN checks — only fire when supplier has at least one ACCPAY bill in window.
        if bucket:
            if not normalise_abn(abn_raw):
                findings.append({
                    "detector": "no-abn",
                    "domain": "ap",
                    "severity": "warning",
                    "entity_code": entity,
                    "title": f"Supplier ABN missing — {name}",
                    "detail": (
                        f"Supplier has {bucket['count']} ACCPAY bill(s) in the last "
                        f"{_env_int('PAYABLES_LOOKBACK_DAYS', lookback_days)} days but "
                        f"no TaxNumber on the Xero Contact. Without a valid ABN, JBC may "
                        f"be required to withhold tax. Capture an ABN before further "
                        f"approvals."
                    ),
                    "amount": round(bucket["total"], 2),
                    "evidence": {
                        "dedupKey": f"no-abn:{entity}:{cid}",
                        "kind": "no-abn",
                        "xeroContactId": cid,
                        "supplierName": name,
                        "openBillCount": bucket["count"],
                    },
                })
            elif not is_valid_abn(abn_raw):
                findings.append({
                    "detector": "invalid-abn",
                    "domain": "ap",
                    "severity": "warning",
                    "entity_code": entity,
                    "title": f"Supplier ABN fails checksum — {name}",
                    "detail": (
                        f'Xero Contact TaxNumber "{abn_raw}" fails the ATO ABN checksum. '
                        f"Either a typo or the ABN does not exist. Confirm with the supplier."
                    ),
                    "amount": round(bucket["total"], 2),
                    "evidence": {
                        "dedupKey": f"invalid-abn:{entity}:{cid}",
                        "kind": "invalid-abn",
                        "xeroContactId": cid,
                        "supplierName": name,
                        "providedAbn": abn_raw,
                    },
                })

        # New-supplier quarantine: Contact Updated inside window AND has a bill in window.
        if bucket:
            updated = parse_xero_date(c.get("UpdatedDateUTC"))
            if updated is not None:
                age_days = (now - updated).total_seconds() / 86_400.0
                if 0 <= age_days <= new_window:
                    findings.append({
                        "detector": "new-supplier-quarantine",
                        "domain": "ap",
                        "severity": "critical",
                        "entity_code": entity,
                        "title": f"New supplier with active bills — {name}",
                        "detail": (
                            f'Supplier "{name}" was added or modified in Xero '
                            f"{age_days:.0f} days ago (within the "
                            f"{new_window}-day new-supplier window) and already has "
                            f"{bucket['count']} ACCPAY bill(s). Per guardrail §2.4, "
                            f"hold any payment run release until a human verifies the "
                            f"supplier and bank details out-of-band."
                        ),
                        "amount": round(bucket["total"], 2),
                        "evidence": {
                            "dedupKey": f"new-supplier-quarantine:{entity}:{cid}",
                            "kind": "new-supplier-quarantine",
                            "xeroContactId": cid,
                            "supplierName": name,
                            "contactUpdatedAt": updated.isoformat(),
                            "ageDays": round(age_days, 1),
                            "openBillCount": bucket["count"],
                        },
                    })

    return findings
