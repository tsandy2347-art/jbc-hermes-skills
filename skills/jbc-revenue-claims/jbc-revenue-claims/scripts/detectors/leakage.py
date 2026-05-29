"""Revenue leakage detectors.

unclaimed-revenue       (warning)  AlayaCare delivered service with no
                                   matching line on any Xero ACCREC invoice
                                   for the same entity.
claim-window-elapsed    (critical) Service unclaimed AND >= window days old.

is_people_flag = TRUE on every emitted finding (participant ref appears).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable


def _today() -> _dt.date:
    return _dt.date.today()


def _parse_iso(s: str) -> _dt.date | None:
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _invoice_external_ids(invoices: list[dict[str, Any]]) -> set[str]:
    """Best-effort: collect tokens that might cross-reference an
    AlayaCare external_id (LineItems.ItemCode, Description, Reference,
    InvoiceNumber). Keeps the match loose since the legacy agent ran
    a similar substring sweep before falling through to ParticipantRef
    + ServiceDate matching.
    """
    tokens: set[str] = set()
    for inv in invoices:
        for key in ("Reference", "InvoiceNumber"):
            v = inv.get(key)
            if isinstance(v, str) and v.strip():
                tokens.add(v.strip())
        for li in inv.get("LineItems") or []:
            for key in ("ItemCode", "Description"):
                v = li.get(key)
                if isinstance(v, str) and v.strip():
                    tokens.add(v.strip())
    return tokens


def run_leakage(
    entity: str,
    services: Iterable[Any],
    invoices: list[dict[str, Any]],
    *,
    window_days: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    today = _today()
    inv_tokens = _invoice_external_ids(invoices)

    for svc in services:
        if getattr(svc, "entity_code", "") != entity:
            continue
        ext = getattr(svc, "external_id", "") or ""
        if not ext:
            continue

        # Naive match: external_id appears verbatim in any invoice token.
        matched = any(ext in t or t in ext for t in inv_tokens if ext and t)
        if matched:
            continue

        svc_date = _parse_iso(getattr(svc, "service_date_iso", "") or "")
        age_days = (today - svc_date).days if svc_date else None
        amount = getattr(svc, "line_total", None)
        if amount is None:
            up = getattr(svc, "unit_price", None) or 0.0
            hrs = getattr(svc, "hours", 0.0) or 0.0
            amount = round(up * hrs, 2) if (up and hrs) else None

        pref = getattr(svc, "participant_ref", "?") or "?"
        program = getattr(svc, "program", "") or "?"
        date_str = getattr(svc, "service_date_iso", "") or "unknown date"
        item = getattr(svc, "support_item_raw", "") or "(item unknown)"
        evidence = {
            "dedupKey": f"unclaimed-revenue:{entity}:{ext}",
            "kind": "unclaimed-revenue",
            "serviceExternalId": ext,
            "participantRef": pref,
            "program": program,
            "serviceDate": date_str,
            "supportItem": item,
            "hours": getattr(svc, "hours", 0.0),
            "ageDays": age_days,
            "trueSahParticipant": getattr(svc, "true_sah_participant", False),
        }
        findings.append({
            "detector": "unclaimed-revenue",
            "domain": "revenue",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": True,
            "title": f"{entity} {program}: unclaimed service for {pref} on {date_str}",
            "detail": (
                f"AlayaCare delivered service {ext} ({item}, "
                f"{getattr(svc, 'hours', 0.0):.2f}h) has no matching Xero "
                f"invoice line. Estimated revenue at risk "
                f"{'$' + format(amount, '.2f') if amount is not None else 'unknown'}."
            ),
            "amount": amount,
            "evidence": evidence,
        })

        if age_days is not None and age_days >= window_days:
            findings.append({
                "detector": "claim-window-elapsed",
                "domain": "revenue",
                "severity": "critical",
                "entity_code": entity,
                "is_people_flag": True,
                "title": f"{entity} {program}: claim window elapsed ({age_days}d) for {pref}",
                "detail": (
                    f"Service {ext} delivered {date_str} is {age_days} days old "
                    f"and still unclaimed (threshold {window_days}d). Either "
                    f"submit immediately or write off — silence at this age is "
                    f"a compliance risk."
                ),
                "amount": amount,
                "evidence": {**evidence,
                             "dedupKey": f"claim-window-elapsed:{entity}:{ext}",
                             "kind": "claim-window-elapsed",
                             "windowDays": window_days},
            })

    return findings
