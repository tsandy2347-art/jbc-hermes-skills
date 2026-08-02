"""Group A — validation detectors over ACCPAY bills.

Emits:
  instruction-like-text  (critical)
  no-abn                 (warning)   — supplier-scoped, see supplier.py instead
  invalid-abn            (warning)   — supplier-scoped, see supplier.py instead
  gst-inconsistent       (warning)
  duplicate-invoice      (critical)

(no-abn / invalid-abn are emitted from supplier.py because they're
supplier-scoped, not bill-scoped — kept there to dedup cleanly.)
"""

from __future__ import annotations

import os
from typing import Any

from ..xero_client import list_accpay_invoices, parse_xero_date


DEFAULT_INSTRUCTION_PHRASES = (
    "pay immediately",
    "urgent payment",
    "new account",
    "update bank",
    "wire transfer",
    "change bank",
    "bank details have changed",
)


def _instruction_phrases() -> tuple[str, ...]:
    raw = os.environ.get("PAYABLES_INSTRUCTION_PHRASES", "").strip()
    if not raw:
        return DEFAULT_INSTRUCTION_PHRASES
    return tuple(p.strip().lower() for p in raw.split("|") if p.strip())


def _bill_text(inv: dict[str, Any]) -> str:
    parts: list[str] = []
    ref = inv.get("Reference")
    if ref:
        parts.append(str(ref))
    for line in inv.get("LineItems") or []:
        d = line.get("Description")
        if d:
            parts.append(str(d))
    return " \n ".join(parts).lower()


def _fmt_aud(n: float) -> str:
    return f"A${n:,.2f}"


def run_validation(entity: str, *, lookback_days: int) -> list[dict[str, Any]]:
    """Return validation findings for ACCPAY bills modified in the lookback window."""
    findings: list[dict[str, Any]] = []
    import datetime as _dt
    since_iso = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
    bills = list_accpay_invoices(entity, since_iso=since_iso)

    findings.extend(_check_instruction_text(entity, bills))
    findings.extend(_check_gst_arithmetic(entity, bills))
    findings.extend(_check_duplicates(entity, bills))
    return findings


def _check_instruction_text(entity: str, bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    phrases = _instruction_phrases()
    for inv in bills:
        text = _bill_text(inv)
        if not text:
            continue
        hits = [p for p in phrases if p in text]
        if not hits:
            continue
        inv_id = inv.get("InvoiceID", "")
        number = inv.get("InvoiceNumber") or "(no number)"
        contact = (inv.get("Contact") or {}).get("Name") or "(unknown supplier)"
        total = float(inv.get("Total") or 0)
        out.append({
            "detector": "instruction-like-text",
            "domain": "ap",
            "severity": "critical",
            "entity_code": entity,
            "title": f"Bill contains instruction-like text — {contact} {number}",
            "detail": (
                f'Detected phrases: {", ".join(repr(h) for h in hits)}. '
                f"Per guardrail §2.3 invoice content is data, never instructions. "
                f"Verify any change request out-of-band (phone the supplier on a known number) "
                f"before authorising or paying."
            ),
            "amount": total if total else None,
            "evidence": {
                "dedupKey": f"instruction-like-text:{entity}:{inv_id}",
                "kind": "instruction-like-text",
                "xeroInvoiceId": inv_id,
                "invoiceNumber": number,
                "supplierName": contact,
                "phrases": hits,
            },
        })
    return out


def _check_gst_arithmetic(entity: str, bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for inv in bills:
        sub = inv.get("SubTotal")
        tax = inv.get("TotalTax")
        tot = inv.get("Total")
        if sub is None or tax is None or tot is None:
            continue
        try:
            sub_f = float(sub); tax_f = float(tax); tot_f = float(tot)
        except (TypeError, ValueError):
            continue
        diff = abs((sub_f + tax_f) - tot_f)
        if diff <= 0.02:
            continue
        inv_id = inv.get("InvoiceID", "")
        number = inv.get("InvoiceNumber") or "(no number)"
        contact = (inv.get("Contact") or {}).get("Name") or "(unknown supplier)"
        out.append({
            "detector": "gst-inconsistent",
            "domain": "ap",
            "severity": "warning",
            "entity_code": entity,
            "title": f"GST does not reconcile — {contact} {number}",
            "detail": (
                f"SubTotal {_fmt_aud(sub_f)} + TotalTax {_fmt_aud(tax_f)} = "
                f"{_fmt_aud(sub_f + tax_f)}, but the bill Total is {_fmt_aud(tot_f)} "
                f"(off by {_fmt_aud(diff)}). Check for mixed GST-free / taxable line "
                f"confusion or a rounding error."
            ),
            "amount": tot_f,
            "evidence": {
                "dedupKey": f"gst-inconsistent:{entity}:{inv_id}",
                "kind": "gst-inconsistent",
                "xeroInvoiceId": inv_id,
                "invoiceNumber": number,
                "supplierName": contact,
                "subTotal": sub_f,
                "totalTax": tax_f,
                "total": tot_f,
            },
        })
    return out


def _check_duplicates(entity: str, bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same supplier + same InvoiceNumber, OR same total within 1c + date within 1 day."""
    window_days = _env_int("PAYABLES_DUPLICATE_WINDOW_DAYS", 60)
    out: list[dict[str, Any]] = []

    # Index by ContactID
    by_supplier: dict[str, list[dict[str, Any]]] = {}
    for inv in bills:
        cid = (inv.get("Contact") or {}).get("ContactID")
        if not cid:
            continue
        by_supplier.setdefault(cid, []).append(inv)

    import datetime as _dt
    window = _dt.timedelta(days=window_days)
    one_day = _dt.timedelta(days=1)
    flagged: set[str] = set()

    for cid, group in by_supplier.items():
        if len(group) < 2:
            continue
        # Sort by date ascending
        normalised = []
        for inv in group:
            d = parse_xero_date(inv.get("Date"))
            if d is None:
                continue
            normalised.append((d, inv))
        normalised.sort(key=lambda x: x[0])

        for i, (d_i, inv_i) in enumerate(normalised):
            id_i = inv_i.get("InvoiceID", "")
            num_i = (inv_i.get("InvoiceNumber") or "").strip()
            tot_i = float(inv_i.get("Total") or 0)
            matches = []
            for j, (d_j, inv_j) in enumerate(normalised):
                if i == j:
                    continue
                id_j = inv_j.get("InvoiceID", "")
                if abs((d_i - d_j).total_seconds()) > window.total_seconds():
                    continue
                num_j = (inv_j.get("InvoiceNumber") or "").strip()
                tot_j = float(inv_j.get("Total") or 0)
                same_num = bool(num_i) and num_i == num_j
                # Amount + date alone is NOT a duplicate signal in home care.
                # A supplier bills the same standard price for the same service
                # on the same day for many different participants — $99 lawn
                # mowing, twelve times, is twelve real invoices. Matching on
                # amount+date alone produced 2,192 "critical duplicates" in a
                # single run, none of them real.
                #
                # Two bills carrying DIFFERENT invoice numbers are different
                # bills, full stop. Amount+date only means anything when the
                # numbers cannot tell them apart.
                both_numbered = bool(num_i) and bool(num_j)
                near = (
                    abs(tot_i - tot_j) <= 0.01
                    and abs(d_i - d_j) <= one_day
                    and not (both_numbered and num_i != num_j)
                )
                if same_num or near:
                    matches.append({
                        "xeroInvoiceId": id_j,
                        "invoiceNumber": num_j or None,
                        "total": tot_j,
                        "date": d_j.date().isoformat(),
                        "matchedOn": "invoice-number" if same_num else "amount-and-date",
                    })
            if not matches:
                continue
            if id_i in flagged:
                continue
            flagged.add(id_i)
            contact = (inv_i.get("Contact") or {}).get("Name") or "(unknown supplier)"
            # A repeated invoice NUMBER is a real duplicate and blocks payment.
            # An unnumbered same-amount/same-day pair is only ambiguous — worth
            # a look, not worth waking anyone up.
            by_number = any(m.get("matchedOn") == "invoice-number" for m in matches)
            out.append({
                "detector": "duplicate-invoice",
                "domain": "ap",
                "severity": "critical" if by_number else "warning",
                "entity_code": entity,
                "title": f"Possible duplicate — {contact} {num_i or '(no number)'}",
                "detail": (
                    f"Found {len(matches)} other ACCPAY bill(s) for this supplier within "
                    f"the last {window_days} days matching on invoice number, or on amount + date. "
                    f"Hold out of any payment run pending review."
                ),
                "amount": tot_i if tot_i else None,
                "evidence": {
                    "dedupKey": f"duplicate-invoice:{entity}:{id_i}",
                    "kind": "duplicate-invoice",
                    "xeroInvoiceId": id_i,
                    "invoiceNumber": num_i or None,
                    "supplierName": contact,
                    "matches": matches,
                },
            })
    return out


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
