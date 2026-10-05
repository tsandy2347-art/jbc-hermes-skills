"""Group B — supplier-scoped detectors.

Emits:
  no-abn                    (warning)  — supplier has open ACCPAY bills but no TaxNumber
  invalid-abn               (warning)  — supplier TaxNumber fails ATO checksum
  new-supplier-quarantine   (critical >= $5k, else warning) — supplier first seen
                                         inside the window, has a bill, and is not
                                         approved in the compliance hub

new-supplier-quarantine, rewritten Oct 2026. It used to treat any supplier whose
Contact UpdatedDateUTC was in the last 30 days as "new" — so long-standing
suppliers re-qualified every time someone edited them — never checked whether
the hub had already vetted the supplier, and was not swept, so flags outlived
their own window (71 open on 5 Oct, 51 older than 30 days, all critical).

Now:
  - "new" = first seen inside PAYABLES_NEW_SUPPLIER_WINDOW_DAYS, from a
    first-seen register (supplier_first_seen in the findings DB). The first
    run seeds it: a contact counts as new only if its earliest bill in the
    look-back is inside the window; otherwise it is treated as long-standing.
  - a supplier APPROVED in the compliance hub (matched by ABN, then name,
    within the entity) is vetted and not flagged — including RETAIL suppliers
    AP quick-adds for utilities and other business suppliers.
  - it is a state detector: the flag clears when the supplier is approved in
    the hub or ages out of the window.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

import re

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


# ── first-seen register + hub vetting ──────────────────────────────

_FIRST_SEEN_DDL = """
    CREATE TABLE IF NOT EXISTS supplier_first_seen (
        entity_code     text NOT NULL,
        contact_xero_id text NOT NULL,
        first_seen_at   timestamptz NOT NULL,
        seeded          boolean NOT NULL DEFAULT false,
        PRIMARY KEY (entity_code, contact_xero_id)
    )
"""

_EPOCH = _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc)


def _norm_name(name: str | None) -> str:
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(pty|ltd|limited|the|t/a|trading as)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _findings_db_url() -> str | None:
    return os.environ.get("JBC_FINDINGS_DATABASE_URL") or os.environ.get(
        "HERMES_FINDINGS_DATABASE_URL"
    )


def _first_seen(entity: str, contacts: list[dict[str, Any]],
                earliest_bill: dict[str, _dt.datetime],
                now: _dt.datetime, window_days: int) -> dict[str, _dt.datetime]:
    """{contact_id: first_seen_at}, registering any contact not yet known."""
    import psycopg

    with psycopg.connect(_findings_db_url()) as conn, conn.cursor() as cur:
        cur.execute(_FIRST_SEEN_DDL)
        cur.execute(
            "SELECT contact_xero_id, first_seen_at FROM supplier_first_seen "
            "WHERE entity_code = %s",
            (entity,),
        )
        known = {cid: ts for cid, ts in cur.fetchall()}
        seeding = not known
        window_start = now - _dt.timedelta(days=window_days)
        new_rows = []
        for c in contacts:
            cid = c.get("ContactID")
            if not cid or cid in known:
                continue
            if seeding:
                # Bootstrap: only a contact whose earliest bill in the look-back
                # falls inside the window looks new; everything else is
                # long-standing. Imperfect for once-a-year suppliers, but it
                # starts the register without flagging the whole ledger.
                eb = earliest_bill.get(cid)
                ts = eb if eb is not None and eb >= window_start else _EPOCH
            else:
                ts = now
            known[cid] = ts
            new_rows.append((entity, cid, ts, seeding))
        if new_rows:
            cur.executemany(
                "INSERT INTO supplier_first_seen (entity_code, contact_xero_id, first_seen_at, seeded) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                new_rows,
            )
        conn.commit()
    return known


_HUB_ENTITY = {"SC": "Sunshine Coast", "CQ": "Central Queensland"}


def _hub_approved(entity: str) -> tuple[set[str], set[str]] | None:
    """(ABNs, normalised names) of suppliers APPROVED in the compliance hub for
    this entity, or None when the hub isn't reachable/configured."""
    url = os.environ.get("COMPLIANCE_DATABASE_URL")
    if not url:
        return None
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT s.abn, s.name FROM "Supplier" s JOIN "Region" r ON r.id = s."supplierRegionId" '
            "WHERE r.name = %s AND s.status = 'APPROVED'",
            (_HUB_ENTITY.get(entity, entity),),
        )
        abns: set[str] = set()
        names: set[str] = set()
        for abn, name in cur.fetchall():
            digits = "".join(ch for ch in (abn or "") if ch.isdigit())
            if digits:
                abns.add(digits)
            n = _norm_name(name)
            if n:
                names.add(n)
    return abns, names


def run_supplier(entity: str, *, lookback_days: int) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    new_window = _env_int("PAYABLES_NEW_SUPPLIER_WINDOW_DAYS", 30)
    since_iso = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()

    contacts = list_supplier_contacts(entity)
    bills = list_accpay_invoices(entity, since_iso=since_iso)

    # Build supplier_id -> (count, total, sample_bill)
    by_supplier: dict[str, dict[str, Any]] = {}
    earliest_bill: dict[str, _dt.datetime] = {}
    for inv in bills:
        cid = (inv.get("Contact") or {}).get("ContactID")
        if not cid:
            continue
        bd = parse_xero_date(inv.get("Date"))
        if bd is not None:
            if bd.tzinfo is None:
                bd = bd.replace(tzinfo=_dt.timezone.utc)
            if cid not in earliest_bill or bd < earliest_bill[cid]:
                earliest_bill[cid] = bd
        b = by_supplier.setdefault(cid, {"count": 0, "total": 0.0, "sample": inv})
        b["count"] += 1
        try:
            b["total"] += float(inv.get("Total") or 0)
        except (TypeError, ValueError):
            pass

    now = _dt.datetime.now(_dt.timezone.utc)
    # Both stores must answer, or the quarantine check is skipped this run with
    # a coverage finding — never guessed. The ABN checks below still run.
    first_seen: dict[str, _dt.datetime] | None = None
    hub: tuple[set[str], set[str]] | None = None
    quarantine_error: str | None = None
    try:
        first_seen = _first_seen(entity, contacts, earliest_bill, now, new_window)
        hub = _hub_approved(entity)
        if hub is None:
            quarantine_error = "COMPLIANCE_DATABASE_URL not set — cannot tell vetted suppliers"
    except Exception as exc:  # noqa: BLE001
        quarantine_error = f"{type(exc).__name__}: {exc}"
    if quarantine_error:
        findings.append({
            "detector": "new-supplier-detector-failed",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "title": f"{entity}: new-supplier check could not run",
            "detail": f"New-supplier quarantine skipped this run: {quarantine_error}.",
            "amount": None,
            "evidence": {
                "dedupKey": f"new-supplier-detector-failed:{entity}",
                "kind": "ingest-failure",
                "error": quarantine_error,
            },
        })

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

        # New-supplier quarantine: first seen inside the window, has a bill,
        # and not approved in the compliance hub.
        if bucket and first_seen is not None and hub is not None:
            seen_at = first_seen.get(cid)
            if seen_at is None:
                continue
            age_days = (now - seen_at).total_seconds() / 86_400.0
            if not (0 <= age_days <= new_window):
                continue
            hub_abns, hub_names = hub
            abn_digits = "".join(ch for ch in (abn_raw or "") if ch.isdigit())
            if (abn_digits and abn_digits in hub_abns) or _norm_name(name) in hub_names:
                continue  # vetted in the compliance hub
            total = round(bucket["total"], 2)
            findings.append({
                "detector": "new-supplier-quarantine",
                "domain": "ap",
                "severity": "critical" if total >= 5000 else "warning",
                "entity_code": entity,
                "title": f"New supplier, not yet approved in the hub — {name}",
                "detail": (
                    f'Supplier "{name}" was first seen {age_days:.0f} days ago and '
                    f"already has {bucket['count']} bill(s) (${total:,.2f}), but is "
                    f"not an approved supplier in the compliance hub. Per guardrail "
                    f"§2.4, verify the supplier and bank details out-of-band before "
                    f"the payment run — then approve it in the hub (care suppliers) "
                    f"or quick-add it as RETAIL (utilities, recruiters, other "
                    f"business suppliers), which clears this flag."
                ),
                "amount": total,
                "evidence": {
                    "dedupKey": f"new-supplier-quarantine:{entity}:{cid}",
                    "kind": "new-supplier-quarantine",
                    "xeroContactId": cid,
                    "supplierName": name,
                    "firstSeenAt": seen_at.isoformat(),
                    "ageDays": round(age_days, 1),
                    "openBillCount": bucket["count"],
                },
            })

    return findings
