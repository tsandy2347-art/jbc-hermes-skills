"""Service-level detectors: unclaimed-service + missing-visit-notes.

`unclaimed-service`: AlayaCare visit with no matching Xero ACCREC invoice
line that carries the visit's externalId reference. Headline leakage $$
estimated from visit hours * unit_price_aud (when present in the CSV);
falls back to line_total_aud.

`missing-visit-notes`: SaH visit with visit_notes_present=false. SaH-only
per evidence-of-visit rule. NDIS visits are not flagged here.
"""

from __future__ import annotations

from typing import Any


def run_unclaimed_service(
    entity: str,
    services: list[Any],
    claimed_service_refs: set[str],
) -> list[dict[str, Any]]:
    """services: list of alayacare_csv.Service for this entity only."""
    out: list[dict[str, Any]] = []
    for svc in services:
        if not svc.external_id:
            continue
        # The Xero side records refs like "VIS-12345" or "SVC-abc"; we
        # match a few common shapes against the AlayaCare externalId.
        candidates = {
            svc.external_id.upper(),
            f"VIS-{svc.external_id}".upper(),
            f"SVC-{svc.external_id}".upper(),
            f"ALC-{svc.external_id}".upper(),
        }
        if claimed_service_refs & candidates:
            continue
        # Projected claim value.
        amount = svc.line_total_aud or (svc.hours * svc.unit_price_aud) or None
        out.append({
            "detector": "unclaimed-service",
            "domain": "unclaimed",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": (
                f"{entity}: unclaimed {svc.program} visit for "
                f"{svc.participant_ref} on {svc.service_date}"
            ),
            "detail": (
                f"AlayaCare visit {svc.external_id} ({svc.hours:.2f}h, "
                f"{svc.support_item_raw or 'item n/a'}) has no matching Xero "
                f"ACCREC line. This is direct revenue leakage — claim or "
                f"explain. Participant ref masked to {svc.participant_ref}."
            ),
            "amount": round(amount, 2) if amount else None,
            "evidence": {
                "dedupKey": f"unclaimed-service:{entity}:{svc.external_id}",
                "kind": "unclaimed-service",
                "program": svc.program,
                "serviceExternalId": svc.external_id,
                "serviceDate": svc.service_date,
                "supportItemRaw": svc.support_item_raw,
                "hours": svc.hours,
                "participantRef": svc.participant_ref,
                "trueSahParticipant": svc.true_sah_participant,
                "csvSource": svc.raw.get("__source_path__"),
            },
        })
    return out


def run_missing_visit_notes(entity: str, services: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for svc in services:
        if svc.program != "SAH":
            continue
        if svc.visit_notes_present:
            continue
        if not svc.external_id:
            continue
        out.append({
            "detector": "missing-visit-notes",
            "domain": "evidence",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": (
                f"{entity}: SaH visit notes missing for {svc.participant_ref} "
                f"on {svc.service_date}"
            ),
            "detail": (
                f"SaH visit {svc.external_id} has no notes recorded. SaH "
                f"evidence-of-visit rule requires notes for every billable "
                f"visit — claim is exposed without them."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"missing-visit-notes:{entity}:{svc.external_id}",
                "kind": "missing-visit-notes",
                "program": "SAH",
                "serviceExternalId": svc.external_id,
                "serviceDate": svc.service_date,
                "participantRef": svc.participant_ref,
                "trueSahParticipant": svc.true_sah_participant,
            },
        })
    return out
