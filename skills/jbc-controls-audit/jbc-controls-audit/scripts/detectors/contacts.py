"""Contacts-domain detectors:
  - no-abn               (aggregate, systemic; one summary finding per entity)
  - vendor-master-change (per-vendor; email / ABN / name changed vs snapshot)

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


# ── vendor-master-change ─────────────────────────────────────────
#
# Rewritten Oct 2026. The old check raised a finding for every supplier whose
# UpdatedDateUTC fell in the last 2 days — any touch, including a brand-new
# supplier being created — and never said what changed. By 5 Oct it had 126
# open rows the brief could only list as names to "confirm authorised".
#
# Now it diffs each supplier against a stored snapshot
# (contact_master_snapshots) and raises only on the fields that matter for
# payment redirection, saying old → new:
#   - EmailAddress  (warning) — where remittances go; the classic redirect
#   - TaxNumber/ABN (warning) — who is actually being paid
#   - Name          (info)    — usually a legitimate rename, worth a glance
# Address / phone edits and first sightings only refresh the snapshot. Bank
# account changes stay with detectors/bank.py (critical, restricted routing).

MASTER_FIELDS = {
    "email": ("EmailAddress", "remittance email", "warning"),
    "abn": ("TaxNumber", "ABN", "warning"),
    "name": ("Name", "name", "info"),
}

_SNAPSHOT_DDL = """
    CREATE TABLE IF NOT EXISTS contact_master_snapshots (
        entity_code     text NOT NULL,
        contact_xero_id text NOT NULL,
        name            text,
        email           text,
        abn             text,
        updated_at_utc  timestamptz,
        captured_at     timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (entity_code, contact_xero_id)
    )
"""


def _db_url() -> str | None:
    return (
        os.environ.get("JBC_FINDINGS_DATABASE_URL")
        or os.environ.get("HERMES_FINDINGS_DATABASE_URL")
    )


def _norm_field(key: str, value: Any) -> str:
    v = ("" if value is None else str(value)).strip()
    if key == "email":
        return v.lower()
    if key == "abn":
        return "".join(ch for ch in v if ch.isdigit())
    return " ".join(v.split())


def _show(key: str, value: str) -> str:
    """How a value appears in the title. Emails keep their domain — the part
    that tells a redirect — but mask the mailbox, since some suppliers are sole
    traders on a personal address and the daily brief is not restricted."""
    if not value:
        return "(blank)"
    if key == "email" and "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    return value


def _current_master(c: dict[str, Any]) -> dict[str, str]:
    return {k: _norm_field(k, c.get(xero_key)) for k, (xero_key, _, _) in MASTER_FIELDS.items()}


def _vendor_master_changes(entity: str, contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diff suppliers against the stored snapshot, then refresh it.

    Raises if the snapshot store is unreachable — the runner turns that into
    a contacts-detector-failed coverage gap rather than silently reporting
    "no changes".
    """
    url = _db_url()
    if not url:
        raise RuntimeError("findings DB URL not set — cannot keep supplier snapshots")
    import psycopg  # the runner's dependency; imported here so ABN checks work without it

    out: list[dict[str, Any]] = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(_SNAPSHOT_DDL)
        cur.execute(
            "SELECT contact_xero_id, name, email, abn FROM contact_master_snapshots "
            "WHERE entity_code = %s",
            (entity,),
        )
        prior = {r[0]: {"name": r[1] or "", "email": r[2] or "", "abn": r[3] or ""}
                 for r in cur.fetchall()}

        rows = []
        for c in contacts:
            contact_id = c.get("ContactID") or ""
            if not contact_id:
                continue
            now_vals = _current_master(c)
            updated = parse_xero_date(c.get("UpdatedDateUTC"))
            rows.append((entity, contact_id, now_vals["name"], now_vals["email"],
                         now_vals["abn"], updated))
            before = prior.get(contact_id)
            if before is None:
                continue  # first sighting — baseline only; new suppliers have their own check
            changes = [
                (k, before[k], now_vals[k])
                for k in MASTER_FIELDS
                if before[k] != now_vals[k]
            ]
            if not changes:
                continue
            name = c.get("Name") or "(unnamed)"
            severity = "warning" if any(MASTER_FIELDS[k][2] == "warning" for k, _, _ in changes) else "info"
            what = "; ".join(
                f"{MASTER_FIELDS[k][1]} {_show(k, old)} → {_show(k, new)}"
                for k, old, new in changes
            )
            fp = _fingerprint(*(f"{k}={new}" for k, _, new in changes))
            out.append({
                "detector": "vendor-master-change",
                "domain": "controls",
                "severity": severity,
                "entity_code": entity,
                "is_people_flag": False,  # vendor master data is systemic
                "title": f"{entity}: supplier details changed — {name}: {what}",
                "detail": (
                    f"Supplier \"{name}\" changed in Xero"
                    + (f" on {updated.isoformat()}" if updated else "")
                    + f": {what}. "
                    + ("A changed remittance email or ABN is how invoice-"
                       "redirection fraud starts — confirm with the supplier on "
                       "a number you already had, not one from the new details."
                       if severity == "warning" else
                       "Usually a legitimate rename; worth a glance.")
                ),
                "amount": None,
                "evidence": {
                    "dedupKey": f"vendor-master-change:{entity}:{contact_id}:{fp}",
                    "kind": "vendor-master-change",
                    "xeroContactId": contact_id,
                    "contactName": name,
                    "updatedAtUtc": updated.isoformat() if updated else None,
                    "changes": [{"field": k, "from": old, "to": new} for k, old, new in changes],
                    "fingerprint": fp,
                },
            })

        cur.executemany(
            """
            INSERT INTO contact_master_snapshots
                (entity_code, contact_xero_id, name, email, abn, updated_at_utc, captured_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (entity_code, contact_xero_id) DO UPDATE
               SET name = EXCLUDED.name, email = EXCLUDED.email, abn = EXCLUDED.abn,
                   updated_at_utc = EXCLUDED.updated_at_utc, captured_at = now()
            """,
            rows,
        )
        conn.commit()
    return out


def run_contacts(entity: str) -> list[dict[str, Any]]:
    """Returns a list of finding-dicts.

    NOTE: these are SYSTEMIC findings — is_people_flag = False. Even
    vendor-master-change uses the vendor's *trading entity* name (a
    business, not a natural person). The orchestrator's emit-time
    routing guard verifies the flag/title invariants.
    """
    findings: list[dict[str, Any]] = []
    today_iso = _dt.date.today().isoformat()
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
    try:
        findings.extend(_vendor_master_changes(entity, contacts))
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "detector": "contacts-detector-failed",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: supplier-change check could not run ({type(exc).__name__})",
            "detail": f"Supplier snapshot store unavailable: {exc}.",
            "amount": None,
            "evidence": {
                "dedupKey": f"contacts-detector-failed:{entity}:master-snapshot",
                "kind": "ingest-failure",
                "error": str(exc),
            },
        })

    return findings
