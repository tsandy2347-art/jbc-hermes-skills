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
import re
from typing import Any

from .. import compliance_db as cdb
from ..xero_controls import list_bills, parse_xero_date

COMPLIANCE_CRITICAL_TYPES = {"PUBLIC_LIABILITY", "POLICE_CHECK"}

# Overhead / non-participant vendors that legitimately never route through the
# compliance hub. A paid Xero bill from one of these is expected to be
# "unlinked" — we skip it rather than flag it. Matched case-insensitively as
# WHOLE WORDS / phrases against the Xero contact name (see _allowlisted). Tune
# via AUDIT_PAID_INVOICE_ALLOWLIST (comma-separated, appended to these defaults).
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


def _keyword_patterns(keywords: tuple[str, ...]) -> list[re.Pattern[str]]:
    return [
        re.compile(r"(?<![a-z0-9])" + re.escape(k.strip()) + r"(?![a-z0-9])")
        for k in keywords
        if k.strip()
    ]


def _allowlisted(contact_name: str | None, patterns: list[re.Pattern[str]]) -> bool:
    """Whole-word match. Substring matching (until Oct 2026) skipped 21 real
    suppliers that merely contained a keyword — "Maroochy Waters" physio
    ("water"), "Brent of All Trades" ("rent"), "Taribelang Aboriginal
    Corporation" ("origin"), "Shelley Lane" ("shell") — so they were never
    checked for vetting at all."""
    name = (contact_name or "").lower()
    return any(p.search(name) for p in patterns)


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
        # The paid-invoice-* detectors are swept (see run_controls_audit
        # STATE_DETECTORS), so returning nothing here would read as "all
        # clear" and close every open one. Say the check did not run instead.
        return [{
            "detector": "paid-invoice-consistency-failed",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: Xero ↔ compliance check skipped — hub DB not configured",
            "detail": "COMPLIANCE_DATABASE_URL is not set, so paid bills could not be matched to tickets.",
            "amount": None,
            "evidence": {
                "dedupKey": f"paid-invoice-consistency-failed:{entity}",
                "kind": "ingest-failure",
            },
        }]

    today_iso = _dt.date.today().isoformat()
    tolerance = _env_float("AUDIT_PAID_INVOICE_TOLERANCE_AUD", 0.05)
    cutoff = _cutoff_date()
    findings: list[dict[str, Any]] = []

    # Bounded recent window. list_bills() fetches it in date slices so the
    # result is complete (SC has ~6,700 bills in 90 days, past Xero's 5,000
    # per-request cap), and raises rather than return a short list — which
    # the except below turns into a skipped-run finding that also stops the
    # sweep. Override via AUDIT_PAID_INVOICE_WINDOW_DAYS.
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
    allowlist = _keyword_patterns(_allowlist())
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
                "dedupKey": f"paid-invoice-consistency-failed:{entity}",
                "kind": "ingest-failure",
                "error": str(exc),
            },
        })
        return findings

    # Unlinked bills and compliance-lapsed invoices are reported per
    # supplier, not per bill (see the emit sections below).
    unlinked: dict[tuple[str, str], dict[str, Any]] = {}
    lapsed_by_supplier: dict[str, dict[str, Any]] = {}

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

        # Fallback: the exact billId link only exists when the hub itself
        # pushed the bill to Xero. A bill keyed in by hand has no billId, so
        # match it back to its ticket on the extracted invoice number instead.
        # Corroborate on amount where both sides have one — an invoice number
        # alone is not unique enough to clear a control finding on.
        matched_by = "billId" if link else None
        if link is None:
            inv_key = cdb.normalise_invoice_no(b.get("InvoiceNumber"))
            cand = snap.tickets_by_invoice_no.get((entity, inv_key)) if inv_key else None
            if cand is not None:
                amounts_agree = (
                    cand.extracted_total is None
                    or bill_total == 0
                    or abs(cand.extracted_total - bill_total) <= max(0.02, bill_total * 0.01)
                )
                if amounts_agree:
                    link = cand
                    matched_by = "invoiceNumber"

        # 6. Unlinked bill — paid/authorised in Xero with no compliance ticket.
        if link is None:
            if cutoff and bill_date and bill_date < cutoff:
                continue  # pre-hub, expected to be unlinked

            norm = _norm_supplier(contact_name)
            is_known = bool(norm) and norm in known_supplier_names
            is_allowlisted = _allowlisted(contact_name, allowlist)

            # Expected-direct overhead (payroll, ATO, rent, utilities, SaaS…) —
            # legitimately never routes through the hub. Skip unless it's a
            # known participant-service supplier (then the bypass still matters).
            if is_allowlisted and not is_known:
                continue

            subkind = "hub-supplier-bypass" if is_known else "unvetted-vendor"
            g = unlinked.setdefault((subkind, norm or contact_name.lower()), {
                "subkind": subkind,
                "contact": contact_name,
                "bills": [],
            })
            g["bills"].append({
                "xeroBillId": bill_id,
                "xeroBillNumber": b.get("InvoiceNumber"),
                "billDate": b.get("Date"),
                "billStatus": b.get("Status"),
                "paid": paid,
                "billTotal": bill_total,
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

        # 2. Amount drift — the ticket's extracted total and the Xero bill
        # disagree. Worded so it does not presume which side is wrong: on
        # 7 Oct 2026 every large drift (Looney's Labour, 4 bills) was the
        # invoice reader putting the pre-GST figure in "total" while Xero held
        # the correct GST-inclusive amount. The ticket figures still matter —
        # they feed the approval card and the AlayaCare markup — so the flag
        # stays, but it names the likely cause instead of implying overpayment.
        if link.extracted_total is not None:
            ext = float(link.extracted_total)
            delta = abs(bill_total - ext)
            if delta > tolerance:
                gst_on_xero = ext > 0 and abs(bill_total - ext * 1.1) <= 0.05
                gst_on_ticket = bill_total > 0 and abs(ext - bill_total * 1.1) <= 0.05
                if gst_on_xero:
                    cause = ("exactly 10% apart — the ticket probably recorded the "
                             "pre-GST amount as the total")
                    sev = "warning"
                elif gst_on_ticket:
                    cause = ("exactly 10% apart — the ticket includes GST the Xero "
                             "bill does not; check whether the supplier charges GST")
                    sev = "warning"
                elif delta < 1.0:
                    cause = "a rounding difference"
                    sev = "info"
                else:
                    cause = "check the invoice to see which is right"
                    sev = "critical" if delta > max(50.0, bill_total * 0.05) else "warning"
                findings.append({
                    "detector": "paid-invoice-amount-drift",
                    "domain": "controls",
                    "severity": sev,
                    "entity_code": entity,
                    "is_people_flag": False,
                    "title": (
                        f"{entity}: ticket figures don't match the Xero bill — "
                        f"#{link.ticket_number} ticket ${ext:,.2f} vs Xero "
                        f"${bill_total:,.2f} ({cause})"
                    ),
                    "detail": (
                        f"Ticket #{link.ticket_number} ({contact_name}) recorded "
                        f"${ext:,.2f}; Xero bill {bill_number} is ${bill_total:,.2f} "
                        f"(difference ${delta:,.2f}) — {cause}. Neither side is "
                        f"assumed correct: check the invoice. If the ticket is "
                        f"wrong, the approval card and any AlayaCare entry keyed "
                        f"from it carry the wrong figure (the markup is calculated "
                        f"from it); if Xero is wrong, correct the bill before "
                        f"payment."
                    ),
                    "amount": round(delta, 2),
                    "evidence": {
                        "dedupKey": (
                            f"paid-invoice-amount-drift:{entity}:"
                            f"{link.ticket_id}:"
                            f"{_fingerprint(bill_total, link.extracted_total)}"
                        ),
                        "kind": "paid-invoice-amount-drift",
                        "ticketId": link.ticket_id,
                        "ticketNumber": link.ticket_number,
                        # "invoiceNumber" here means the bill was keyed into
                        # Xero by hand rather than pushed by the hub — useful
                        # when judging how a drift arose.
                        "matchedBy": matched_by,
                        "xeroBillId": bill_id,
                        "extractedTotal": link.extracted_total,
                        "xeroTotal": bill_total,
                        "delta": round(delta, 2),
                        "likelyCause": (
                            "ticket-ex-gst" if gst_on_xero else
                            "ticket-has-gst" if gst_on_ticket else
                            "rounding" if delta < 1.0 else "unknown"
                        ),
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
                g = lapsed_by_supplier.setdefault(link.supplier_id, {
                    "fallbackName": link.supplier_name_extracted,
                    "types": {},
                    "invoices": [],
                })
                for r in lapsed:
                    g["types"][r.type] = r.date_due
                g["invoices"].append({
                    "ticketId": link.ticket_id,
                    "ticketNumber": link.ticket_number,
                    "invoiceDate": link.invoice_date.isoformat(),
                    "xeroBillId": bill_id,
                    "billTotal": bill_total,
                    "lapsedTypes": sorted(r.type for r in lapsed),
                })

    # 4 (emit). Compliance lapsed — ONE finding per supplier, not per invoice.
    # Until Oct 2026 each invoice was its own line (28 for Colomba alone on
    # 5 Oct). The check compares each invoice date with the supplier's CURRENT
    # expiry dates in the hub, so a supplier that renews drops out on the next
    # run; anything still listed is expired today, and is said so plainly.
    for sid, g in lapsed_by_supplier.items():
        supplier = snap.suppliers.get(sid)
        supplier_name = supplier.name if supplier else (g["fallbackName"] or "(unknown)")
        invoices = sorted(g["invoices"], key=lambda r: r["invoiceDate"])
        n = len(invoices)
        total = round(sum(r["billTotal"] for r in invoices), 2)
        types = sorted(g["types"].items(), key=lambda kv: kv[1])
        critical = any(t in COMPLIANCE_CRITICAL_TYPES for t, _ in types)
        expired = ", ".join(
            f"{t.replace('_', ' ').title()} (expired {d.date().isoformat()})" for t, d in types
        )
        first = invoices[0]["invoiceDate"][:10]
        last = invoices[-1]["invoiceDate"][:10]
        span = first if first == last else f"{first} to {last}"
        findings.append({
            "detector": "paid-invoice-compliance-lapsed",
            "domain": "controls",
            "severity": "critical" if critical else "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": (
                f"{entity}: paying a supplier with expired compliance — "
                f"{supplier_name}: {n} invoice{'s' if n != 1 else ''} "
                f"(${total:,.0f}) after {expired}"
            ),
            "detail": (
                f"{n} invoice(s) from \"{supplier_name}\" dated {span}, totalling "
                f"${total:,.2f}, were processed after: {expired}. Still expired "
                f"in the compliance hub today. Either get the renewed documents "
                f"into the hub (this clears on the next run) or hold further "
                f"payments. Tickets: "
                + ", ".join(f"#{r['ticketNumber']}" for r in invoices[-20:])
                + ("…" if n > 20 else "")
                + "."
            ),
            "amount": total,
            "evidence": {
                "dedupKey": f"paid-invoice-compliance-lapsed:{entity}:{sid}",
                "kind": "paid-invoice-compliance-lapsed",
                "supplierId": sid,
                "supplierName": supplier_name,
                "invoiceCount": n,
                "total": total,
                "lapsed": [{"type": t, "dateDue": d.isoformat()} for t, d in types],
                "invoices": invoices[-50:],
            },
        })

    # 6 (emit). Unlinked bills — ONE finding per supplier, not per bill.
    # Until Oct 2026 each bill was its own finding and none ever closed, so
    # one supplier (JBC's own CBD & North Brisbane office, a recruiter, a
    # broadband provider) filled the brief with a line per payment going back
    # months. The group covers the AUDIT_PAID_INVOICE_WINDOW_DAYS window and
    # is swept: it clears when the supplier is vetted in the hub, is
    # allowlisted / marked "not a care supplier" from the brief, or its bills
    # age out of the window.
    for (subkind, norm_key), g in unlinked.items():
        contact_name = g["contact"]
        rows = sorted(g["bills"], key=lambda r: str(r.get("billDate") or ""))
        total = round(sum(r["billTotal"] for r in rows), 2)
        n = len(rows)
        if subkind == "hub-supplier-bypass":
            title = (
                f"{entity}: hub supplier paid in Xero with no ticket — "
                f"{contact_name} ({n} bill{'s' if n != 1 else ''}, ${total:,.0f})"
            )
            detail = (
                f"\"{contact_name}\" is an approved compliance-hub supplier, but "
                f"{n} bill(s) totalling ${total:,.2f} in the last {window_days} days "
                f"were created/paid in Xero with no ticket — they bypassed "
                f"care-partner approval and the supplier compliance gate. Confirm "
                f"they were legitimately approved outside the hub."
            )
        else:
            title = (
                f"{entity}: payments to unvetted vendor — {contact_name} "
                f"({n} bill{'s' if n != 1 else ''}, ${total:,.0f})"
            )
            detail = (
                f"{n} bill(s) totalling ${total:,.2f} in the last {window_days} days "
                f"went to \"{contact_name}\", which is not a compliance-hub "
                f"supplier and has no tickets. If it supplies participant "
                f"services it skipped vetting; if it is a business supplier, "
                f"tap \"not a care supplier\" on the brief link (or quick-add it "
                f"in the hub as RETAIL) and it will stop appearing."
            )
        findings.append({
            "detector": "paid-invoice-unlinked",
            "domain": "controls",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": title,
            "detail": detail,
            "amount": total,
            "evidence": {
                "dedupKey": f"paid-invoice-unlinked:{entity}:{subkind}:{norm_key}",
                "kind": "paid-invoice-unlinked",
                "subkind": subkind,
                "xeroContactName": contact_name,
                "knownHubSupplier": subkind == "hub-supplier-bypass",
                "billCount": n,
                "billTotal": total,
                "bills": rows[-50:],
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
        # This check is about two Xero bills existing for one invoice, so it
        # only considers tickets the hub actually pushed. Tickets with no
        # XERO_UPLOADED event carry no bill id (and no upload timestamp to
        # order by) and are not duplicates of anything in Xero.
        if not lnk.xero_bill_id:
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
