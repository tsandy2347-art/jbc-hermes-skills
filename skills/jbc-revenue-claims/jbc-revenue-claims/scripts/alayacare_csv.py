"""AlayaCare delivered-services CSV reader for jbc-revenue-claims.

READ-ONLY. AlayaCare has no usable API for delivered-services — CSV drop
is the only ingest path. Missing/unset path is a graceful no-op handled
by the caller (run_revenue_claims.main).

Schema (best-effort header aliasing):
    externalId | externalid | serviceid              -> external_id
    entityCode | entitycode                          -> entity_code  (SC|CQ)
    participantName | participantnameraw | client    -> participant_name_raw
    program                                          -> program      (NDIS|SAH)
    serviceDate | servicedateiso | date              -> service_date_iso
    supportItem | supportitemraw | item              -> support_item_raw
    hours                                            -> hours
    visitNotesPresent | notespresent                 -> visit_notes_present
    unitPrice | price                                -> unit_price (optional)
    lineTotal | amount                               -> line_total (optional)
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Service:
    external_id: str
    entity_code: str
    participant_ref: str          # masked initials-XXXX
    participant_name_raw: str     # kept in-memory only; never persisted
    true_sah_participant: bool
    program: str                  # NDIS | SAH | ""
    service_date_iso: str
    support_item_raw: str
    hours: float
    visit_notes_present: bool
    unit_price: float | None
    line_total: float | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadResult:
    path: str
    missing: bool
    services: list[Service]


_HEADER_ALIASES = {
    "externalid": "external_id",
    "serviceid": "external_id",
    "entitycode": "entity_code",
    "entity": "entity_code",
    "participantname": "participant_name_raw",
    "participantnameraw": "participant_name_raw",
    "client": "participant_name_raw",
    "participant": "participant_name_raw",
    "program": "program",
    "fundingstream": "program",
    "servicedate": "service_date_iso",
    "servicedateiso": "service_date_iso",
    "date": "service_date_iso",
    "supportitem": "support_item_raw",
    "supportitemraw": "support_item_raw",
    "item": "support_item_raw",
    "linecode": "support_item_raw",
    "hours": "hours",
    "qty": "hours",
    "quantity": "hours",
    "visitnotespresent": "visit_notes_present",
    "notespresent": "visit_notes_present",
    "notes": "visit_notes_present",
    "unitprice": "unit_price",
    "price": "unit_price",
    "rate": "unit_price",
    "linetotal": "line_total",
    "amount": "line_total",
    "total": "line_total",
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _to_float(s: str | None) -> float | None:
    if s is None or str(s).strip() == "":
        return None
    s = str(s).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _to_bool(s: str | None) -> bool:
    if s is None:
        return False
    v = str(s).strip().lower()
    return v in ("1", "true", "t", "y", "yes")


def _entity_norm(raw: str) -> str:
    e = (raw or "").strip().upper()
    if e in ("SC", "CQ"):
        return e
    low = e.lower()
    if e.startswith("CQ") or "central" in low or "queensland" in low:
        return "CQ"
    if e.startswith("SC") or "sunshine" in low:
        return "SC"
    return ""


def _program_norm(raw: str) -> str:
    p = (raw or "").strip().upper()
    if p in ("NDIS", "SAH"):
        return p
    low = p.lower()
    if "ndis" in low:
        return "NDIS"
    if "sah" in low or "support at home" in low or "support-at-home" in low:
        return "SAH"
    # HCP is fully wound down — do not emit.
    return ""


def mask_participant_ref(name_raw: str) -> tuple[str, bool, str]:
    """Returns (masked_ref, true_sah_flag, cleaned_name).

    Strips trailing `+` (true-SaH marker) and `*` (unconfirmed; NOT
    discharged — ignored). Masks to `initials-XXXX` (SHA1 suffix of
    cleaned name).
    """
    name = (name_raw or "").strip()
    true_sah = False
    # Strip markers (may be multiple trailing).
    while name and name[-1] in ("+", "*"):
        if name[-1] == "+":
            true_sah = True
        name = name[:-1].rstrip()

    if not name:
        return ("unknown-0000", false_if_unset := False, "")  # type: ignore[name-defined]
    parts = [p for p in name.split() if p]
    initials = "".join(p[0] for p in parts).upper()[:4] or "??"
    digest = hashlib.sha1(name.lower().encode("utf-8")).hexdigest()[:4].upper()
    return (f"{initials}-{digest}", true_sah, name)


def load(path: str) -> LoadResult:
    if not path or not os.path.exists(path):
        return LoadResult(path=path or "", missing=True, services=[])

    services: list[Service] = []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        canonical: dict[str, str] = {}
        for h in reader.fieldnames or []:
            n = _norm(h)
            canonical[h] = _HEADER_ALIASES.get(n, n)
        for raw_row in reader:
            row = {canonical.get(k, k): (v if v is not None else "") for k, v in raw_row.items()}
            entity = _entity_norm(row.get("entity_code") or "")
            if not entity:
                continue
            program = _program_norm(row.get("program") or "")
            name_raw = (row.get("participant_name_raw") or "").strip()
            ref, true_sah, cleaned = mask_participant_ref(name_raw)
            services.append(Service(
                external_id=(row.get("external_id") or "").strip(),
                entity_code=entity,
                participant_ref=ref,
                participant_name_raw=cleaned,
                true_sah_participant=true_sah,
                program=program,
                service_date_iso=(row.get("service_date_iso") or "").strip()[:10],
                support_item_raw=(row.get("support_item_raw") or "").strip(),
                hours=_to_float(row.get("hours")) or 0.0,
                visit_notes_present=_to_bool(row.get("visit_notes_present")),
                unit_price=_to_float(row.get("unit_price")),
                line_total=_to_float(row.get("line_total")),
                raw={k: (v if isinstance(v, str) else "") for k, v in row.items()},
            ))
    return LoadResult(path=path, missing=False, services=services)
