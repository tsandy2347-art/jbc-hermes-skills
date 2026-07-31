"""Paid-invoice-consistency detector — Xero bills × jbc-compliance tickets.

Six sub-checks per entity, all SYSTEMIC (no people-flag):
  1. paid_but_returned     — Xero bill paid + RETURNED_TO_CP event landed
                              after the upload                       (critical)
  2. amount_drift          — Xero bill Total vs extracted total       (warn/crit)
  3. wrong_entity          — ticket entity ≠ Xero tenant the bill is in (warn)
  4. compliance_lapsed     — supplier compliance expired at invoice date
                                                                      (warn/crit)
  5. duplicate_xero_bill   — ≥2 LIVE Xero bills for one supplier sharing the
                              same Xero invoice number AND total     (critical)
  6. unlinked_bill         — Xero bill ≥ AUDIT_COMPLIANCE_LINK_CUTOFF
                              with no matching XERO_UPLOADED event   (info)

The join is exact: TicketEvent.data->>'billId' ↔ Xero InvoiceID. Bills
older than AUDIT_COMPLIANCE_LINK_CUTOFF (default 2026-04-22, hub go-live)
are skipped from the unlinked check — they pre-date the hub and never
had a link.

No COMPLIANCE_DATABASE_URL set ⇒ this detector is a silent no-op (returns
empty list). Failures inside the detector still surface via the
orchestrator's `_ingest_failure` wrapper.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Any

from .. import compliance_db as cdb
from ..xero_controls import list_bills, parse_xero_date

COMPLIANCE_CRITICAL_TYPES = {"PUBLIC_LIABILITY", "POLICE_CHECK"}

# Overhead / non-participant vendors that legitimately never route through the
# compliance hub. A paid Xero bill from one of these is expected to be
# "unlinked" — we skip it rather than flag it. Matched as case-insensitive
# substrings against the Xero contact name. Tune via AUDIT_PAID_INVOICE_ALLOWLIST
# (comma-separated, appended to these defaults).
DEFAULT_ALLOWLIST_KEYWORDS = (
    "payroll", "wages", "superannuation", "super fund", "ato",
    "australian taxation", "office of state revenue", "osr ",
    "rent", "lease", "body corporate", "real estate",
    "electricity", "energy", "ergon", "origin", "agl", "water",
    "telstra", "optus", "vodafone", "internet", "nbn",
    "railway", "amazon web", "aws", "anthropic", "openai",
    "microsoft", "google", "xero", "adobe", "atlassian",
    "insurance", "workcover", "bank", "westpac", "nab", "commonwealth",
    "fuel", "ampol", "bp ", "shell", "toll", "auspost", "australia post",
)


def _allowlist() -> tuple[str, ...]:
    extra = os.environ.get("AUDIT_PAID_INVOICE_ALLOWLIST", "")
    extras = tuple(
        k.strip().lower() for k in extra.split(",") if k.strip()
    )
    return DEFAULT_ALLOWLIST_KEYWORDS + extras


def _norm_supplier(name: str | None) -> str:
    """Normalise a supplier/contact name for cross-system matching: lowercase,
    drop common company suffixes + punctuation, collapse whitespace."""
    if not name:
        return ""
    s = str(name).lower()
    for suffix in (" pty ltd", " pty. ltd.", " pty ltd.", " p/l", " ltd",
                   " limited", " inc", " incorporated", " t/a", " trading as"):
        s = s.replace(suffix, " ")
    s = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in s)
    return " ".join(s.split())


def _fingerprint(*parts: Any) -> str:
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


def _bill_total(b: dict[str, Any]) -> float:
    return float(b.get("Total") or 0)


def _is_paid(b: dict[str, Any]) -> bool:
    return b.get("Status") == "PAID" or float(b.get("AmountPaid") or 0) > 0


def _cutoff_date() -> _dt.date | None:
    raw = os.environ.get("AUDIT_COMPLIANCE_LINK_CUTOFF", "2026-04-22")
    try:
        return _dt.date.fromisoformat(raw)
    except ValueError:
        return None


# Module-level cache so SC + CQ runs share one load of the compliance DB.
_SNAPSHOT_CACHE: cdb.ComplianceSnapshot | None = None
_SNAPSHOT_LOADED = False


def _get_snapshot() -> cdb.ComplianceSnapshot | None:
    global _SNAPSHOT_CACHE, _SNAPSHOT_LOADED  # noqa: PLW0603
    if _SNAPSHOT_LOADED:
        return _SNAPSHOT_CACHE
    _SNAPSHOT_LOADED = True
    if not cdb.configured():
        _SNAPSHOT_CACHE = None
        return None
    try:
        _SNAPSHOT_CACHE = cdb.load_snapshot()
    except Exception:  # noqa: BLE001
        # Bubble up via the orchestrator's _ingest_failure path on first call,
        # but stay None on subsequent ones so we don't hammer a broken DB.
        _SNAPSHOT_CACHE = None
        raise
    return _SNAPSHOT_CACHE


def run_paid_invoice_consistency(entity: str) -> list[dict[str, Any]]:
    snap = _get_snapshot()
    if snap is None:
        return []

    today_iso = _dt.date.today().isoformat()
    tolerance = _env_float("AUDIT_PAID_INVOICE_TOLERANCE_AUD", 0.05)
    cutoff = _cutoff_date()
    findings: list[dict[str, Any]] = []

    # Bounded recent window. list_bills() now pulls newest-first and caps at
    # 5000 rows; a tight window keeps every recent bill inside the cap (a
    # 365-day window on a high-volume tenant pushed recent bills past the cap
    # and the detector never saw them). 90 days covers normal supplier-pay
    # cycles with headroom. Override via AUDIT_PAID_INVOICE_WINDOW_DAYS.
    window_days = int(_env_float("AUDIT_PAID_INVOICE_WINDOW_DAYS", 90))
    today = _dt.date.today()
    from_iso = (today - _dt.timedelta(days=window_days)).isoformat()
    to_iso = today.isoformat()

    # Index of known hub suppliers (normalised name) for this entity, plus the
    # overhead allowlist — used to classify an unlinked bill as a hub-supplier
    # bypass vs an unvetted vendor vs an expected-direct overhead.
    known_supplier_names = {
        _norm_supplier(s.name)
        for s in snap.suppliers.values()
        if s.entity_code == entity and s.name
    }
    known_supplier_names.discard("")
    allowlist = _allowlist()
    try:
        bills = list_bills(entity, from_iso=from_iso, to_iso=to_iso)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "detector": "paid-invoice-consistency-failed",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: bills pull failed ({type(exc).__name__})",
            "detail": f"Xero Invoices endpoint (ACCPAY) failed: {exc}.",
            "amount": None,
            "evidence": {
                "dedupKey": f"paid-invoice-consistency-failed:{entity}:{today_iso}",
                "kind": "ingest-failure",
                "error": str(exc),
            },
        })
        return findings

    # ── per-bill checks ───────────────────────────────────────────
    for b in bills:
        if b.get("Status") in ("VOIDED", "DELETED", "DRAFT"):
            continue
        bill_id = b.get("InvoiceID")
        if not bill_id:
            continue
        bill_date_dt = parse_xero_date(b.get("Date"))
        bill_date = bill_date_dt.date() if bill_date_dt else None
        bill_total = _bill_total(b)
        bill_number = b.get("InvoiceNumber") or "(no #)"
        contact_name = (b.get("Contact") or {}).get("Name", "(unknown)")
        paid = _is_paid(b)

        link = snap.tickets_by_billid.get((entity, bill_id))

        # 6. Unlinked bill — paid/authorised in Xero with no compliance ticket.
        if link is None:
            if cutoff and bill_date and bill_date < cutoff:
                continue  # pre-hub, expected to be unlinked

            norm = _norm_supplier(contact_name)
            is_known = bool(norm) and norm in known_supplier_names
            is_allowlisted = any(kw in (contact_name or "").lower()
                                 for kw in allowlist)

            # Expected-direct overhead (payroll, ATO, rent, utilities, SaaS…) —
            # legitimately never routes through the hub. Skip unless it's a
            # known participant-service supplier (then the bypass still matters).
            if is_allowlisted and not is_known:
                continue

            if is_known:
                subkind = "hub-supplier-bypass"
                title = (
                    f"{entity}: hub supplier paid in Xero with no ticket — "
                    f"{contact_name} (${bill_total:.0f})"
                )
                detail = (
                    f"\"{contact_name}\" is an approved compliance-hub supplier, "
                    f"but bill {bill_number} for ${bill_total:.2f} (status "
                    f"{b.get('Status')}) was created/paid in Xero with no ticket "
                    f"— it bypassed care-partner approval and the supplier "
                    f"compliance gate. Confirm it was legitimately approved "
                    f"outside the hub."
                )
            else:
                subkind = "unvetted-vendor"
                title = (
                    f"{entity}: payment to unvetted vendor — "
                    f"{contact_name} (${bill_total:.0f})"
                )
                detail = (
                    f"Bill {bill_number} for ${bill_total:.2f} (status "
                    f"{b.get('Status')}) was paid to \"{contact_name}\", which "
                    f"is not in the compliance hub as a supplier and has no "
                    f"ticket. If this is a participant expense it skipped "
                    f"compliance vetting entirely; if it's a genuine overhead, "
                    f"add it to AUDIT_PAID_INVOICE_ALLOWLIST to silence."
                )

            findings.append({
                "detector": "paid-invoice-unlinked",
                "domain": "controls",
                "severity": "warning",
                "entity_code": entity,
                "is_people_flag": False,
                "title": title,
                "detail": detail,
                "amount": bill_total,
                "evidence": {
                    "dedupKey": f"paid-invoice-unlinked:{entity}:{bill_id}",
                    "kind": "paid-invoice-unlinked",
                    "subkind": subkind,
                    "xeroBillId": bill_id,
                    "xeroBillNumber": b.get("InvoiceNumber"),
                    "xeroContactName": contact_name,
                    "knownHubSupplier": is_known,
                    "billDate": b.get("Date"),
                    "billStatus": b.get("Status"),
                    "paid": paid,
                    "billTotal": bill_total,
                },
            })
            continue

        # 1. Paid + returned to CP after upload
        if paid and link.returned_after_xero:
            findings.append({
                "detector": "paid-invoice-paid-but-returned",
                "domain": "controls",
                "severity": "critical",
                "entity_code": entity,
                "is_people_flag": False,
                "title": (
                    f"{entity}: paid in Xero after ticket returned to CP — "
                    f"#{link.ticket_number} {contact_name}"
                ),
                "detail": (
                    f"Ticket #{link.ticket_number} ({contact_name}, bill "
                    f"{bill_number}, ${bill_total:.2f}) was returned to the "
                    f"care partner AFTER the Xero draft bill was created, "
                    f"yet the bill is paid. Likely a CP rejection that "
                    f"didn't reflect in Xero — confirm with Finance before "
                    f"the supplier banks the funds."
                ),
                "amount": bill_total,
                "evidence": {
                    "dedupKey": (
                        f"paid-invoice-paid-but-returned:{entity}:"
                        f"{link.ticket_id}:{bill_id}"
                    ),
                    "kind": "paid-invoice-paid-but-returned",
                    "ticketId": link.ticket_id,
                    "ticketNumber": link.ticket_number,
                    "xeroBillId": bill_id,
                    "xeroBillNumber": b.get("InvoiceNumber"),
                    "amountPaid": float(b.get("AmountPaid") or 0),
                    "billTotal": bill_total,
                },
            })

        # 2. Amount drift
        if link.extracted_total is not None:
            delta = abs(bill_total - float(link.extracted_total))
            if delta > tolerance:
                sev = "critical" if delta > max(50.0, bill_total * 0.05) else "warning"
                findings.append({
                    "detector": "paid-invoice-amount-drift",
                    "domain": "controls",
                    "severity": sev,
                    "entity_code": entity,
                    "is_people_flag": False,
                    "title": (
                        f"{entity}: amount drift — ticket #{link.ticket_number} "
                        f"extracted ${link.extracted_total:.2f} vs Xero "
                        f"${bill_total:.2f}"
                    ),
                    "detail": (
                        f"Invoice extracted from \"{contact_name}\" on ticket "
                        f"#{link.ticket_number} totalled ${link.extracted_total:.2f}; "
                        f"Xero bill {bill_number} is for ${bill_total:.2f} "
                        f"(Δ ${delta:.2f}). Confirm whether the bill was "
                        f"edited after creation."
                    ),
                    "amount": bill_total,
                    "evidence": {
                        "dedupKey": (
                            f"paid-invoice-amount-drift:{entity}:"
                            f"{link.ticket_id}:"
                            f"{_fingerprint(bill_total, link.extracted_total)}"
                        ),
                        "kind": "paid-invoice-amount-drift",
                        "ticketId": link.ticket_id,
                        "ticketNumber": link.ticket_number,
                        "xeroBillId": bill_id,
                        "extractedTotal": link.extracted_total,
                        "xeroTotal": bill_total,
                        "delta": round(delta, 2),
                    },
                })

        # 3. Wrong entity (ticket on SC, bill in CQ — or vice versa)
        if link.entity_code != entity:
            findings.append({
                "detector": "paid-invoice-wrong-entity",
                "domain": "controls",
                "severity": "warning",
                "entity_code": entity,
                "is_people_flag": False,
                "title": (
                    f"{entity}: cross-entity bill — ticket raised against "
                    f"{link.entity_code}"
                ),
                "detail": (
                    f"Ticket #{link.ticket_number} was raised against "
                    f"{link.entity_code} but ended up as Xero bill {bill_number} "
                    f"in {entity} (${bill_total:.2f}, \"{contact_name}\"). "
                    f"Cross-charge by mistake — confirm and journal to the "
                    f"right entity."
                ),
                "amount": bill_total,
                "evidence": {
                    "dedupKey": (
                        f"paid-invoice-wrong-entity:{entity}:"
                        f"{link.ticket_id}:{bill_id}"
                    ),
                    "kind": "paid-invoice-wrong-entity",
                    "ticketEntityCode": link.entity_code,
                    "ticketId": link.ticket_id,
                    "ticketNumber": link.ticket_number,
                    "xeroBillId": bill_id,
                },
            })

        # 4. Supplier compliance lapsed at invoice date
        if link.supplier_id and link.invoice_date:
            comp_rows = snap.compliance_by_supplier.get(link.supplier_id, [])
            lapsed: list[cdb.ComplianceRow] = []
            inv_date = link.invoice_date
            if inv_date.tzinfo is None:
                inv_date = inv_date.replace(tzinfo=_dt.timezone.utc)
            for r in comp_rows:
                if r.not_applicable or not r.date_due:
                    continue
                due = r.date_due
                if due.tzinfo is None:
                    due = due.replace(tzinfo=_dt.timezone.utc)
                if due < inv_date:
                    lapsed.append(r)
            if lapsed:
                critical = any(r.type in COMPLIANCE_CRITICAL_TYPES for r in lapsed)
                supplier = snap.suppliers.get(link.supplier_id)
                supplier_name = supplier.name if supplier else (
                    link.supplier_name_extracted or "(unknown)"
                )
                lapsed_list = ", ".join(
                    f"{r.type} (due {r.date_due.date().isoformat()})" for r in lapsed
                )
                fp = _fingerprint(*(r.type for r in lapsed))
                findings.append({
                    "detector": "paid-invoice-compliance-lapsed",
                    "domain": "controls",
                    "severity": "critical" if critical else "warning",
                    "entity_code": entity,
                    "is_people_flag": False,
                    "title": (
                        f"{entity}: supplier compliance lapsed at invoice date "
                        f"— {supplier_name}"
                    ),
                    "detail": (
                        f"Supplier \"{supplier_name}\" had {len(lapsed)} "
                        f"compliance item(s) expired on the invoice date "
                        f"{link.invoice_date.date().isoformat()} when ticket "
                        f"#{link.ticket_number} was processed "
                        f"(${bill_total:.2f}). Lapsed: {lapsed_list}. "
                        f"Confirm whether evidence existed offline."
                    ),
                    "amount": bill_total,
                    "evidence": {
                        "dedupKey": (
                            f"paid-invoice-compliance-lapsed:{entity}:"
                            f"{link.ticket_id}:{fp}"
                        ),
                        "kind": "paid-invoice-compliance-lapsed",
                        "ticketId": link.ticket_id,
                        "ticketNumber": link.ticket_number,
                        "supplierId": link.supplier_id,
                        "supplierName": supplier_name,
                        "invoiceDate": link.invoice_date.isoformat(),
                        "lapsed": [
                            {"type": r.type, "dateDue": r.date_due.isoformat()}
                            for r in lapsed
                        ],
                    },
                })

    # 5. Duplicate Xero bills across multiple tickets in this entity.
    #
    # Ground truth is the Xero bill, NOT the ticket's extracted invoice number.
    # Two tickets routinely carry the same extracted number while pointing at
    # genuinely different bills (mis-extraction, or a supplier reusing a number
    # months apart), which produced a run of false "double-pay" criticals. So:
    #   - resolve each ticket to the bill it actually created;
    #   - drop bills that are VOIDED/DELETED/DRAFT — a duplicate that has
    #     already been reversed is not an open exposure;
    #   - drop bills outside the pulled window — unverifiable, never flag;
    #   - group on the Xero InvoiceNumber *and* the Xero total, so two
    #     different invoices sharing a number are not called a double-pay;
    #   - dedupe by bill id, because several tickets can point at one bill.
    live_bills = {
        b["InvoiceID"]: b
        for b in bills
        if b.get("InvoiceID")
        and b.get("Status") not in ("VOIDED", "DELETED", "DRAFT")
    }
    by_key: dict[tuple[str, str, str], dict[str, cdb.TicketLink]] = {}
    for lnk in snap.all_links:
        if lnk.entity_code != entity or not lnk.supplier_id:
            continue
        bill = live_bills.get(lnk.xero_bill_id)
        if bill is None:
            continue
        xero_num = (bill.get("InvoiceNumber") or "").strip().lower()
        if not xero_num:
            continue
        key = (lnk.supplier_id, xero_num, f"{_bill_total(bill):.2f}")
        by_key.setdefault(key, {})[lnk.xero_bill_id] = lnk
    for (sid, inum, unit_key), links_by_bill in by_key.items():
        distinct_bill_ids = set(links_by_bill)
        if len(distinct_bill_ids) < 2:
            continue
        supplier = snap.suppliers.get(sid) if sid else None
        supplier_name = supplier.name if supplier else "(unknown)"
        sorted_links = sorted(
            links_by_bill.values(), key=lambda l: l.xero_uploaded_at
        )
        # Exposure is the value of the EXTRA copies, not the sum of all of
        # them — one of these bills is the legitimate one.
        unit_total = float(unit_key)
        total = unit_total * (len(distinct_bill_ids) - 1)
        fp = _fingerprint(*sorted(distinct_bill_ids))
        findings.append({
            "detector": "paid-invoice-duplicate-bill",
            "domain": "controls",
            "severity": "critical",
            "entity_code": entity,
            "is_people_flag": False,
            "title": (
                f"{entity}: same supplier invoice uploaded to Xero "
                f"{len(distinct_bill_ids)}× — {supplier_name} #{inum}"
            ),
            "detail": (
                f"Xero holds {len(distinct_bill_ids)} live bills for supplier "
                f"\"{supplier_name}\" with the same invoice number \"{inum}\" "
                f"and the same total (${unit_total:.2f} each), created via "
                f"separate compliance tickets. Exposure is ${total:.2f} — the "
                f"value of the extra copies. Voided and deleted bills are "
                f"excluded, so these are all still open. "
                f"Tickets: " + ", ".join(f"#{l.ticket_number}" for l in sorted_links)
                + "."
            ),
            "amount": total,
            "evidence": {
                "dedupKey": (
                    f"paid-invoice-duplicate-bill:{entity}:{sid}:{fp}"
                ),
                "kind": "paid-invoice-duplicate-bill",
                "supplierId": sid,
                "supplierName": supplier_name,
                # Xero's invoice number, not the ticket's extracted one.
                "invoiceNumber": inum,
                "unitTotal": unit_total,
                "exposure": total,
                "uploads": [
                    {
                        "ticketId": l.ticket_id,
                        "ticketNumber": l.ticket_number,
                        "xeroBillId": l.xero_bill_id,
                        "xeroBillStatus": (
                            live_bills[l.xero_bill_id].get("Status")
                        ),
                        "xeroBillNumber": (
                            live_bills[l.xero_bill_id].get("InvoiceNumber")
                        ),
                        "xeroBillTotal": _bill_total(live_bills[l.xero_bill_id]),
                        "uploadedAt": l.xero_uploaded_at.isoformat()
                        if l.xero_uploaded_at else None,
                        "extractedTotal": l.extracted_total,
                    }
                    for l in sorted_links
                ],
            },
        })

    return findings
