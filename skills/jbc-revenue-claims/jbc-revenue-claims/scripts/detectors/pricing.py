"""Pricing detectors.

ndis-price-mismatch  (warning)   Xero invoice line priced above PAPL cap.
sah-cap-breach       (critical)  Xero invoice line priced above SaH cap.

In v0.1.0 the price catalogue is not yet surfaced via a config table
(see SKILL.md "Deliberately skipped"). The detector ships as a working
hook that takes an optional `price_caps` dict — when empty (the
default), no findings are emitted. Callers can pre-populate the dict
from environment / config in the future without touching the detector.

is_people_flag = FALSE — pricing findings are line-level, not person-level.
"""

from __future__ import annotations

from typing import Any


def _line_program(line: dict[str, Any], invoice: dict[str, Any]) -> str:
    """Best-effort program classification from item code / account code."""
    item = (line.get("ItemCode") or "").upper()
    acct = (line.get("AccountCode") or "").upper()
    desc = (line.get("Description") or "").upper()
    blob = " ".join((item, acct, desc))
    if "NDIS" in blob:
        return "NDIS"
    if "SAH" in blob or "SUPPORT AT HOME" in blob or "SUPPORT-AT-HOME" in blob:
        return "SAH"
    return ""


def run_pricing(
    entity: str,
    invoices: list[dict[str, Any]],
    *,
    price_caps: dict[str, float] | None = None,
    papl_version: str = "2025-26 v1.1",
    sah_version: str = "SaH 2025-11 v1",
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    caps = price_caps or {}
    if not caps:
        # Single info finding noting catalogue absence — fires daily until
        # PricingRuleSet is loaded.
        findings.append({
            "detector": "ndis-price-mismatch",
            "domain": "revenue",
            "severity": "info",
            "entity_code": entity,
            "is_people_flag": False,
            "title": f"{entity}: pricing catalogue not loaded — cap checks skipped",
            "detail": (
                "PricingRuleSet (NDIS PAPL + SaH price guide) is not yet "
                "surfaced via a config table. ndis-price-mismatch and "
                "sah-cap-breach will not fire until the catalogue is loaded. "
                "Tracking ref: SKILL.md 'Deliberately skipped'."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"pricing-catalogue-missing:{entity}",
                "kind": "pricing-catalogue-missing",
                "paplVersion": papl_version,
                "sahVersion": sah_version,
            },
        })
        return findings

    for inv in invoices:
        inv_id = inv.get("InvoiceID") or inv.get("InvoiceNumber") or "?"
        for li in inv.get("LineItems") or []:
            item = (li.get("ItemCode") or "").strip()
            if not item:
                continue
            cap = caps.get(item)
            if cap is None:
                continue
            unit = li.get("UnitAmount")
            try:
                unit_f = float(unit) if unit is not None else None
            except (TypeError, ValueError):
                unit_f = None
            if unit_f is None or unit_f <= cap + 0.01:
                continue

            program = _line_program(li, inv)
            line_id = li.get("LineItemID") or item
            over = round(unit_f - cap, 2)
            qty = li.get("Quantity") or 0
            try:
                qty_f = float(qty)
            except (TypeError, ValueError):
                qty_f = 0.0
            exposure = round(over * qty_f, 2) if qty_f else over

            if program == "SAH":
                findings.append({
                    "detector": "sah-cap-breach",
                    "domain": "revenue",
                    "severity": "critical",
                    "entity_code": entity,
                    "is_people_flag": False,
                    "title": f"{entity} SaH: cap breach on {item}",
                    "detail": (
                        f"Invoice {inv_id} line {item} priced at "
                        f"${unit_f:.2f} — exceeds SaH cap ${cap:.2f} by "
                        f"${over:.2f}. Quantity {qty_f} → ${exposure:.2f} exposure."
                    ),
                    "amount": exposure,
                    "evidence": {
                        "dedupKey": f"sah-cap-breach:{entity}:{inv_id}:{line_id}",
                        "kind": "sah-cap-breach",
                        "xeroInvoiceId": inv_id,
                        "lineItemId": line_id,
                        "itemCode": item,
                        "unitAmount": unit_f,
                        "cap": cap,
                        "sahVersion": sah_version,
                    },
                })
            else:
                findings.append({
                    "detector": "ndis-price-mismatch",
                    "domain": "revenue",
                    "severity": "warning",
                    "entity_code": entity,
                    "is_people_flag": False,
                    "title": f"{entity} NDIS: price above PAPL cap on {item}",
                    "detail": (
                        f"Invoice {inv_id} line {item} priced at "
                        f"${unit_f:.2f} — exceeds PAPL {papl_version} cap "
                        f"${cap:.2f} by ${over:.2f}. Quantity {qty_f} → "
                        f"${exposure:.2f} exposure."
                    ),
                    "amount": exposure,
                    "evidence": {
                        "dedupKey": f"ndis-price-mismatch:{entity}:{inv_id}:{line_id}",
                        "kind": "ndis-price-mismatch",
                        "xeroInvoiceId": inv_id,
                        "lineItemId": line_id,
                        "itemCode": item,
                        "unitAmount": unit_f,
                        "cap": cap,
                        "paplVersion": papl_version,
                    },
                })
    return findings
