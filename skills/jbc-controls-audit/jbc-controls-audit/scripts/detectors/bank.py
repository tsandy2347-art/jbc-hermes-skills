"""Bank-detail change detector — PEOPLE-FLAGGED.

Classic invoice-redirection fraud signal: a real supplier's bank-account
string changes. Replaces the legacy ContactBankSnapshot baseline table
with a dedupKey fingerprinted on the current bank string. When the bank
string changes, the fingerprint changes, a new finding is emitted; the
prior open finding stays until a human resolves it.

PEOPLE-FLAG INVARIANTS (enforced here AND re-checked at emit time):
  - is_people_flag = True
  - title contains masked initials form '<initials>-XXXX', NOT the full name
  - evidence.individualName has the full vendor name
  - evidence.isRestricted = True
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from ..xero_controls import (
    list_contacts,
    mask_account,
    parse_xero_date,
    people_masked_label,
)


def _fingerprint(*parts: Any) -> str:
    blob = json.dumps([("" if p is None else str(p)) for p in parts],
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def run_bank(entity: str) -> list[dict[str, Any]]:
    """Surface every supplier whose bank-detail string is currently set.

    The dedupKey carries a fingerprint of the bank string — so the first
    sighting per (contact, bank-string) emits exactly once, and changing
    the bank string produces a fresh finding. No prior-snapshot table
    needed.
    """
    findings: list[dict[str, Any]] = []
    today_iso = _dt.date.today().isoformat()

    try:
        contacts = list_contacts(entity, suppliers_only=True)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "detector": "bank-detector-failed",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: bank-detail pull failed ({type(exc).__name__})",
            "detail": f"Xero Contacts endpoint failed: {exc}.",
            "amount": None,
            "evidence": {
                "dedupKey": f"bank-detector-failed:{entity}:{today_iso}",
                "kind": "ingest-failure",
                "error": str(exc),
            },
        })
        return findings

    for c in contacts:
        bank = (c.get("BankAccountDetails") or "").strip()
        if not bank:
            continue
        contact_id = c.get("ContactID") or ""
        name = c.get("Name") or "(unnamed)"
        masked = mask_account(bank)
        fp = _fingerprint(bank)
        label = people_masked_label(name)
        updated = parse_xero_date(c.get("UpdatedDateUTC"))

        findings.append({
            "detector": "bank-detail-change",
            "domain": "controls",
            "severity": "warning",  # critical only when we can prove a *change*
            "entity_code": entity,
            "is_people_flag": True,
            # PEOPLE-FLAG: full name MUST NOT appear in title. Mask form only.
            "title": (
                f"{entity}: vendor bank-detail on file (restricted) — "
                f"{label} ending {masked}"
            ),
            "detail": (
                f"A supplier's bank account on file is recorded as ending "
                f"{masked}. Classic invoice-redirection fraud signal: if "
                f"this fingerprint changes, the finding will re-emit with a "
                f"fresh dedupKey. Vendor identity is restricted — see "
                f"`evidence.individualName` if you are authorised."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"bank-detail-change:{entity}:{contact_id}:{fp}",
                "kind": "bank-detail-change",
                "isRestricted": True,
                "individualName": name,
                "xeroContactId": contact_id,
                "bankMasked": masked,
                "bankFingerprint": fp,
                "contactUpdatedAtUtc": updated.isoformat() if updated else None,
            },
        })

    return findings
