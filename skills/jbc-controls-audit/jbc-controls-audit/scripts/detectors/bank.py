"""Bank-detail change detector — PEOPLE-FLAGGED.

Classic invoice-redirection fraud signal: a real supplier's bank-account
string changes. We treat the DB itself as the baseline — for each contact,
if we have NEVER recorded a fingerprint, we silently bootstrap (write a
resolved baseline marker so future runs have a comparison). Only when an
EXISTING contact's fingerprint actually differs do we emit a critical
people-flagged finding.

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
import os
from typing import Any

from ..xero_controls import (
    list_contacts,
    mask_account,
    parse_xero_date,
    people_masked_label,
)

try:
    import psycopg
except ImportError:
    psycopg = None  # type: ignore[assignment]
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError:
        psycopg2 = None  # type: ignore[assignment]


def _fingerprint(*parts: Any) -> str:
    blob = json.dumps([("" if p is None else str(p)) for p in parts],
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _db_url() -> str | None:
    return (
        os.environ.get("JBC_FINDINGS_DATABASE_URL")
        or os.environ.get("HERMES_FINDINGS_DATABASE_URL")
    )


def _prior_fingerprints(entity: str) -> dict[str, str]:
    """Return {contact_id: latest_seen_bank_fingerprint} from the DB.

    Reads the entire historical set of bank-detail-change + bank-detail-baseline
    rows for this entity. The latest fingerprint per contact wins.
    """
    url = _db_url()
    if not url:
        return {}
    sql = """
        SELECT evidence->>'xeroContactId' AS cid,
               evidence->>'bankFingerprint' AS fp,
               created_at
          FROM findings
         WHERE source_agent = 'controls-audit'
           AND entity_code = %s
           AND detector IN ('bank-detail-change', 'bank-detail-baseline')
           AND evidence ? 'xeroContactId'
         ORDER BY created_at ASC
    """
    seen: dict[str, str] = {}
    try:
        if psycopg is not None:
            with psycopg.connect(url) as conn, conn.cursor() as cur:
                cur.execute(sql, (entity,))
                for row in cur.fetchall():
                    cid, fp, _ = row
                    if cid and fp:
                        seen[cid] = fp  # later rows overwrite — newest wins
        else:
            import psycopg2  # type: ignore[import-not-found]
            with psycopg2.connect(url) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, (entity,))
                    for row in cur.fetchall():
                        cid, fp, _ = row
                        if cid and fp:
                            seen[cid] = fp
    except Exception:  # noqa: BLE001
        # Read-only best-effort: if the DB query fails, treat it as no priors.
        # The orchestrator will surface an ingest finding separately.
        return {}
    return seen


def run_bank(entity: str) -> list[dict[str, Any]]:
    """Emit a finding only when an existing contact's bank fingerprint
    actually changed. On first sighting, write a SILENT baseline marker
    (a resolved finding with detector='bank-detail-baseline') so future
    runs have a comparison anchor.

    Two finding shapes can be emitted:
      - bank-detail-baseline (info, resolved=True, NOT a people-flag risk):
        bootstrapped state for a contact we'd never seen before. These
        don't surface to the user — they exist purely as comparison
        anchors.
      - bank-detail-change (critical, people-flag): an existing baseline's
        fingerprint differs from the current Xero state. This is the
        fraud signal.
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
                "dedupKey": f"bank-detector-failed:{entity}",
                "kind": "ingest-failure",
                "error": str(exc),
            },
        })
        return findings

    priors = _prior_fingerprints(entity)

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

        prior_fp = priors.get(contact_id)
        if prior_fp is None:
            # First sighting — bootstrap a silent baseline. Resolved=true so
            # Mark's open-findings query ignores it; it lives purely as a
            # comparison anchor for tomorrow's run.
            findings.append({
                "detector": "bank-detail-baseline",
                "domain": "controls",
                "severity": "info",
                "entity_code": entity,
                "is_people_flag": False,  # baseline marker, no individual reveal
                "title": f"{entity}: bank-detail baseline established (1 vendor)",
                "detail": (
                    "Silent baseline anchor — future runs compare against this. "
                    "Not actionable. Auto-resolved at emit."
                ),
                "amount": None,
                "evidence": {
                    "dedupKey": f"bank-detail-baseline:{entity}:{contact_id}",
                    "kind": "bank-detail-baseline",
                    "xeroContactId": contact_id,
                    "bankFingerprint": fp,
                    "bootstrappedAt": today_iso,
                },
                "resolved": True,  # silent on the dashboard
            })
            continue

        if prior_fp == fp:
            # No change — nothing to emit. dedupKey UPSERT would noop anyway.
            continue

        # CHANGE DETECTED — this is the fraud signal.
        findings.append({
            "detector": "bank-detail-change",
            "domain": "controls",
            "severity": "critical",
            "entity_code": entity,
            "is_people_flag": True,
            # PEOPLE-FLAG: full name MUST NOT appear in title. Mask form only.
            "title": (
                f"{entity}: vendor bank-detail CHANGED (restricted) — "
                f"{label} now ending {masked}"
            ),
            "detail": (
                f"A supplier's bank account on file has changed. The prior "
                f"fingerprint was {prior_fp}; the current is {fp} ending "
                f"{masked}. Classic invoice-redirection fraud signal — verify "
                f"the change by independently contacting the supplier via a "
                f"known-good channel before paying. Vendor identity is "
                f"restricted — see `evidence.individualName` if you are "
                f"authorised."
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
                "priorBankFingerprint": prior_fp,
                "contactUpdatedAtUtc": updated.isoformat() if updated else None,
            },
        })

    return findings
