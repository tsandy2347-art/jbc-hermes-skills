"""Elevated Xero user roster — aggregate, systemic.

One info-level finding per entity, listing every Xero user with elevated
rights (OrganisationRole == FINANCIALADVISER, or IsSubscriber == true).

is_people_flag = False — this is a *roster summary*, not a finding about
a single named individual. We mask each entry to initials in the title /
detail, but include full names in evidence for the audit trail. The
dedupKey carries the date so the roster is re-stated daily as a
governance trail (prior days stay in the DB until resolved).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..xero_controls import initials, list_users


def _is_elevated(u: dict[str, Any]) -> bool:
    role = (u.get("OrganisationRole") or "").upper()
    if role == "FINANCIALADVISER":
        return True
    if u.get("IsSubscriber") is True:
        return True
    return False


def run_users(entity: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    today_iso = _dt.date.today().isoformat()

    try:
        users = list_users(entity)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "detector": "users-detector-failed",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: users pull failed ({type(exc).__name__})",
            "detail": f"Xero Users endpoint failed: {exc}.",
            "amount": None,
            "evidence": {
                "dedupKey": f"users-detector-failed:{entity}:{today_iso}",
                "kind": "ingest-failure",
                "error": str(exc),
            },
        })
        return findings

    elevated = [u for u in users if _is_elevated(u)]
    if not elevated:
        return findings

    masked_entries = []
    full_entries = []
    for u in elevated:
        full_name = " ".join(
            p for p in [u.get("FirstName"), u.get("LastName")] if p
        ).strip() or u.get("EmailAddress") or "(unknown)"
        masked_entries.append(initials(full_name))
        full_entries.append({
            "userXeroId": u.get("UserID"),
            "individualName": full_name,
            "email": u.get("EmailAddress"),
            "organisationRole": u.get("OrganisationRole"),
            "isSubscriber": bool(u.get("IsSubscriber")),
        })

    findings.append({
        "detector": "elevated-user-roster",
        "domain": "controls",
        "severity": "info",
        "entity_code": entity,
        "is_people_flag": False,  # roster summary, not per-person flag
        "title": (
            f"{entity}: {len(elevated)} elevated Xero user(s) on roster "
            f"({', '.join(masked_entries)})"
        ),
        "detail": (
            f"{len(elevated)} user(s) in the {entity} Xero org currently "
            f"hold FINANCIALADVISER role or IsSubscriber=true. Confirm "
            f"these access levels remain appropriate. Full roster in "
            f"evidence.users (audit trail)."
        ),
        "amount": None,
        "evidence": {
            "dedupKey": f"elevated-user-roster:{entity}:{today_iso}",
            "kind": "elevated-user-roster",
            "count": len(elevated),
            "users": full_entries,
        },
    })

    return findings
