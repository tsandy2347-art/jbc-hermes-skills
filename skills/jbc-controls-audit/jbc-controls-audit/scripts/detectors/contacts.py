"""Contacts-domain detectors:
  - no-abn               (aggregate, systemic; one summary finding per entity)
  - vendor-master-change (per-vendor; SYSTEMIC — vendor names go in evidence)

ABN check: ATO algorithm. 11 digits, subtract 1 from leading digit,
weight by [10,1,3,5,7,9,11,13,15,17,19], sum mod 89 == 0.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Any

from ..xero_controls import list_contacts, parse_xero_date

ABN_WEIGHTS = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]


def is_valid_abn(raw: str | None) -> bool:
    if not raw:
        return False
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 11:
        return False
    d = [int(c) for c in digits]
    d[0] -= 1
    if d[0] < 0:
        return False
    return sum(x * w for x, w in zip(d, ABN_WEIGHTS)) % 89 == 0


def _fingerprint(*parts: Any) -> str:
    """Stable short hash of a canonical tuple, for dedupKey suffixes."""
    blob = json.dumps([("" if p is None else str(p)) for p in parts],
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def run_contacts(entity: str) -> list[dict[str, Any]]:
    """Returns a list of finding-dicts.

    NOTE: these are SYSTEMIC findings — is_people_flag = False. Even
    vendor-master-change uses the vendor's *trading entity* name (a
    business, not a natural person). The orchestrator's emit-time
    routing guard verifies the flag/title invariants.
    """
    findings: list[dict[str, Any]] = []
    today_iso = _dt.date.today().isoformat()
    lookback_days = _env_int("AUDIT_VENDOR_UPDATED_LOOKBACK_DAYS", 2)
    warning_aud = _env_float("AUDIT_NO_ABN_WARNING_AUD", 5000.0)

    try:
        contacts = list_contacts(entity, suppliers_only=True)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "detector": "contacts-detector-failed",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: contacts pull failed ({type(exc).__name__})",
            "detail": f"Xero Contacts endpoint failed: {exc}.",
            "amount": None,
            "evidence": {
                "dedupKey": f"contacts-detector-failed:{entity}",
                "kind": "ingest-failure",
                "error": str(exc),
            },
        })
        return findings

    # ── no-abn (aggregate) ────────────────────────────────────────
    no_abn: list[dict[str, Any]] = []
    invalid_abn: list[dict[str, Any]] = []
    for c in contacts:
        # We don't have spend totals (not pulling invoices in v0.1). All
        # active suppliers with no ABN are surfaced; severity scales with
        # the *count* of offenders rather than per-vendor spend.
        raw = (c.get("TaxNumber") or "").strip() or None
        if raw and is_valid_abn(raw):
            continue
        entry = {
            "contactId": c.get("ContactID"),
            "name": c.get("Name"),
            "providedAbn": raw,
        }
        (invalid_abn if raw else no_abn).append(entry)

    total_offenders = len(no_abn) + len(invalid_abn)
    if total_offenders:
        severity = "warning" if total_offenders >= 5 else "info"
        findings.append({
            "detector": "no-abn",
            "domain": "controls",
            "severity": severity,
            "entity_code": entity,
            "is_people_flag": False,
            "title": (
                f"{entity}: {total_offenders} active supplier(s) missing "
                f"or failing ABN check"
            ),
            "detail": (
                f"{len(no_abn)} supplier(s) have no ABN on file and "
                f"{len(invalid_abn)} have an ABN that fails the ATO checksum. "
                f"Without a valid ABN, JBC may be required to withhold tax. "
                f"Sample of affected supplier names is in evidence.sample."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"no-abn-aggregate:{entity}",
                "kind": "no-abn-aggregate",
                "missingCount": len(no_abn),
                "invalidCount": len(invalid_abn),
                "sample": (no_abn + invalid_abn)[:20],
                "thresholdWarningAud": warning_aud,
            },
        })

    # ── vendor-master-change (per-vendor, systemic) ───────────────
    # We surface every supplier whose UpdatedDateUTC is within the
    # last `lookback_days`. dedupKey includes a fingerprint of the
    # canonical master tuple — when fields change again, fingerprint
    # changes, a new row is emitted.
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(days=lookback_days)
    for c in contacts:
        updated = parse_xero_date(c.get("UpdatedDateUTC"))
        if updated is None or updated < cutoff:
            continue
        contact_id = c.get("ContactID") or ""
        name = c.get("Name") or "(unnamed)"
        fp = _fingerprint(
            name,
            (c.get("TaxNumber") or "").strip(),
            (c.get("BankAccountDetails") or "").strip(),
            (c.get("EmailAddress") or "").strip(),
            json.dumps(c.get("Addresses") or [], sort_keys=True),
            json.dumps(c.get("Phones") or [], sort_keys=True),
        )
        findings.append({
            "detector": "vendor-master-change",
            "domain": "controls",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,  # vendor master data is systemic
            "title": (
                f"{entity}: vendor master data updated — {name}"
            ),
            "detail": (
                f"Supplier \"{name}\" was modified in Xero on "
                f"{updated.isoformat()}. Confirm the change was "
                f"authorised. (Bank-detail changes have a separate "
                f"finding with restricted routing.)"
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"vendor-master-change:{entity}:{contact_id}:{fp}",
                "kind": "vendor-master-change",
                "xeroContactId": contact_id,
                "contactName": name,
                "updatedAtUtc": updated.isoformat(),
                "fingerprint": fp,
            },
        })

    return findings
