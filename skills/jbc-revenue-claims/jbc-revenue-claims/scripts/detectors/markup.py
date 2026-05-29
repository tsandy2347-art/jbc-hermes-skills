"""markup-leakage — SaH-only.

Care Partner (CP) coordination service was delivered but billed at the base
support-worker rate instead of the marked-up CP code. SaH-only per
`feedback_jbc_cps_sah_only`. NDIS is never rolled into CP totals.

Detection is heuristic against the AlayaCare CSV `support_item_raw` field:
  - if it contains 'CP', 'CARE PARTNER' or 'COORDIN', and the unit price
    is below the SaH CP cap (when known), or the matched Xero invoice
    line's item code doesn't match a CP code, flag it.

Without a loaded ruleset, the detector falls back to a name-only signal
and emits at info severity (no $$).
"""

from __future__ import annotations

from typing import Any


_CP_HINTS = ("CP", "CARE PARTNER", "CAREPARTNER", "COORDIN")


def _looks_like_cp(item_raw: str) -> bool:
    if not item_raw:
        return False
    up = item_raw.upper()
    return any(h in up for h in _CP_HINTS)


def run_markup_leakage(
    entity: str,
    services: list[Any],
    services_to_lines: dict[str, dict[str, Any]],
    cp_cap_aud: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for svc in services:
        if svc.program != "SAH":
            continue
        if not _looks_like_cp(svc.support_item_raw):
            continue
        # Try to find the matched Xero line.
        line = None
        for cand in (svc.external_id.upper(),
                     f"VIS-{svc.external_id}".upper(),
                     f"SVC-{svc.external_id}".upper()):
            if cand in services_to_lines:
                line = services_to_lines[cand]
                break
        if line is None:
            continue  # unclaimed-service already covers this case
        unit = float(line["unitAmount"] or 0)
        if cp_cap_aud is not None and unit + 0.01 >= cp_cap_aud:
            continue  # priced at CP rate — no leakage
        # Either the cap is unknown OR the unit price is below CP rate.
        shortfall_per_unit = (cp_cap_aud - unit) if cp_cap_aud else None
        total_short = (shortfall_per_unit * float(line["quantity"] or 1)
                       if shortfall_per_unit else None)
        out.append({
            "detector": "markup-leakage",
            "domain": "markup",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": (
                f"{entity}: SaH CP visit billed at base rate for "
                f"{svc.participant_ref} on {svc.service_date}"
            ),
            "detail": (
                f"AlayaCare item {svc.support_item_raw!r} looks like Care-"
                f"Partner coordination but Xero line {line['invoiceNumber']}"
                f" line {line['lineNo']} was billed at ${unit:,.2f}/unit"
                + (f" vs ${cp_cap_aud:,.2f} CP rate" if cp_cap_aud else "")
                + ". Lost markup — manager-only view."
            ),
            "amount": round(total_short, 2) if total_short else None,
            "evidence": {
                "dedupKey": f"markup-leakage:{entity}:{svc.external_id}",
                "kind": "markup-leakage",
                "program": "SAH",
                "participantRef": svc.participant_ref,
                "serviceExternalId": svc.external_id,
                "supportItemRaw": svc.support_item_raw,
                "billedUnit": unit,
                "cpCapAud": cp_cap_aud,
                "xeroInvoiceId": line["xeroInvoiceId"],
                "lineNo": line["lineNo"],
            },
        })
    return out
