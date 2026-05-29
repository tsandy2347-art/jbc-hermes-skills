"""Claim-level detectors against Xero ACCREC.

- orphan-claim: invoice line referencing an external service id we don't
  see in the AlayaCare CSV.
- duplicate-claim: >1 non-VOIDED invoice line for the same service ref.
- claim-rejected: invoice VOIDED with a service ref (categorise / write-off).
- claim-short-paid: open invoice with partial payment > 1c gap.
- batch-ready-for-release: placeholder gated on an internal batch table
  we no longer carry — emitted as info when CLAIM_AUTO_SUBMIT_BATCH_CEILING_AUD
  is configured but no batch state is queryable.
"""

from __future__ import annotations

from typing import Any


def run_orphan_claim(
    entity: str,
    claim_lines: list[dict[str, Any]],
    known_service_refs: set[str],
) -> list[dict[str, Any]]:
    """known_service_refs: union of all uppercased candidate refs from CSV."""
    out: list[dict[str, Any]] = []
    for line in claim_lines:
        if line["status"] in ("VOIDED", "DELETED"):
            continue
        refs = line.get("serviceRefs") or []
        if not refs:
            continue  # not a claim line — internal sales
        if any(r in known_service_refs for r in refs):
            continue
        out.append({
            "detector": "orphan-claim",
            "domain": "validation",
            "severity": "critical",
            "entity_code": entity,
            "is_people_flag": True,
            "title": (
                f"{entity}: invoice {line['invoiceNumber']} line {line['lineNo']} "
                f"references unknown service {refs[0]}"
            ),
            "detail": (
                f"Xero ACCREC line references service id(s) {refs} but those ids "
                f"are not in the AlayaCare delivered-service export. Potential "
                f"false claim — investigate before any further submission."
            ),
            "amount": round(line["lineAmount"], 2) if line["lineAmount"] else None,
            "evidence": {
                "dedupKey": f"orphan-claim:{entity}:{line['xeroInvoiceId']}:{line['lineNo']}",
                "kind": "orphan-claim",
                "xeroInvoiceId": line["xeroInvoiceId"],
                "invoiceNumber": line["invoiceNumber"],
                "lineNo": line["lineNo"],
                "serviceRefs": refs,
                "status": line["status"],
                "lineAmount": line["lineAmount"],
            },
        })
    return out


def run_duplicate_claim(
    entity: str, claim_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for line in claim_lines:
        if line["status"] in ("VOIDED", "DELETED"):
            continue
        for r in (line.get("serviceRefs") or []):
            by_ref.setdefault(r, []).append(line)
    out: list[dict[str, Any]] = []
    for ref, lines in by_ref.items():
        if len(lines) <= 1:
            continue
        total = sum(l["lineAmount"] for l in lines)
        first = lines[0]
        out.append({
            "detector": "duplicate-claim",
            "domain": "validation",
            "severity": "critical",
            "entity_code": entity,
            "is_people_flag": True,
            "title": f"{entity}: duplicate claim for service {ref} ({len(lines)} lines)",
            "detail": (
                f"Service {ref} appears on {len(lines)} non-voided Xero ACCREC "
                f"lines totalling ${total:,.2f}. Cancel the duplicates before the "
                f"next submission tick or expect a part-paid / rejected outcome."
            ),
            "amount": round(total, 2),
            "evidence": {
                "dedupKey": f"duplicate-claim:{entity}:{ref}",
                "kind": "duplicate-claim",
                "serviceRef": ref,
                "lineCount": len(lines),
                "totalAmount": total,
                "invoices": [
                    {"xeroInvoiceId": l["xeroInvoiceId"],
                     "invoiceNumber": l["invoiceNumber"],
                     "lineNo": l["lineNo"],
                     "amount": l["lineAmount"]}
                    for l in lines[:10]
                ],
                "firstInvoiceId": first["xeroInvoiceId"],
            },
        })
    return out


def run_rejected_claims(
    entity: str, claim_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen_invoices: set[str] = set()
    out: list[dict[str, Any]] = []
    for line in claim_lines:
        if line["status"] != "VOIDED":
            continue
        if not (line.get("serviceRefs") or []):
            continue
        inv_id = line["xeroInvoiceId"]
        if inv_id in seen_invoices:
            continue
        seen_invoices.add(inv_id)
        out.append({
            "detector": "claim-rejected",
            "domain": "outcome",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": f"{entity}: voided claim invoice {line['invoiceNumber']}",
            "detail": (
                "Xero ACCREC invoice carrying a service reference is VOIDED. "
                "Categorise the rejection reason and either resubmit or "
                "write-off — never leave silent."
            ),
            "amount": round(line["invoiceTotal"], 2) if line["invoiceTotal"] else None,
            "evidence": {
                "dedupKey": f"claim-rejected:{entity}:{inv_id}",
                "kind": "claim-rejected",
                "xeroInvoiceId": inv_id,
                "invoiceNumber": line["invoiceNumber"],
                "invoiceTotal": line["invoiceTotal"],
                "serviceRefs": line.get("serviceRefs") or [],
            },
        })
    return out


def run_short_paid(
    entity: str, claim_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen_invoices: set[str] = set()
    out: list[dict[str, Any]] = []
    for line in claim_lines:
        if line["status"] not in ("AUTHORISED", "SUBMITTED"):
            continue
        if line["invoiceAmountPaid"] <= 0:
            continue
        gap = line["invoiceTotal"] - (
            line["invoiceAmountPaid"] + 0  # AmountCredited not surfaced here
        )
        if gap <= 0.01:
            continue
        if not (line.get("serviceRefs") or []):
            continue
        inv_id = line["xeroInvoiceId"]
        if inv_id in seen_invoices:
            continue
        seen_invoices.add(inv_id)
        out.append({
            "detector": "claim-short-paid",
            "domain": "outcome",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": f"{entity}: part-paid claim invoice {line['invoiceNumber']}",
            "detail": (
                f"Claim invoice was paid ${line['invoiceAmountPaid']:,.2f} of "
                f"${line['invoiceTotal']:,.2f} — gap ${gap:,.2f}. Categorise "
                f"the shortfall (price cap, partial reject, plan exhausted) and "
                f"decide between top-up claim or write-off."
            ),
            "amount": round(gap, 2),
            "evidence": {
                "dedupKey": f"claim-short-paid:{entity}:{inv_id}",
                "kind": "claim-short-paid",
                "xeroInvoiceId": inv_id,
                "invoiceNumber": line["invoiceNumber"],
                "invoiceTotal": line["invoiceTotal"],
                "amountPaid": line["invoiceAmountPaid"],
                "gap": gap,
                "serviceRefs": line.get("serviceRefs") or [],
            },
        })
    return out


def run_batch_ready_for_release(entity: str, ceiling_aud: float | None) -> list[dict[str, Any]]:
    """We no longer carry the source agent's ClaimBatch table. We emit a
    single info-level placeholder per entity if a ceiling is configured,
    so Mark surfaces the un-ported gating decision."""
    if not ceiling_aud:
        return []
    return [{
        "detector": "batch-ready-for-release",
        "domain": "validation",
        "severity": "info",
        "entity_code": entity,
        "is_people_flag": False,
        "title": f"{entity}: claim-batch release gating not wired",
        "detail": (
            f"CLAIM_AUTO_SUBMIT_BATCH_CEILING_AUD is set to ${ceiling_aud:,.2f} "
            f"but the skill no longer carries the ClaimBatch table. The "
            f"submission domain is intentionally not ported — surface this so "
            f"the un-ported gating decision stays visible."
        ),
        "evidence": {
            "dedupKey": f"batch-ready-for-release:{entity}:plumbing",
            "kind": "plumbing-gap",
            "ceilingAud": ceiling_aud,
        },
    }]
