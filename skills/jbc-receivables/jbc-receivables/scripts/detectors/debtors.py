"""Debtor-aggregate detectors.

debtor-exposure-breach (critical) — single debtor outstanding > limit.
deteriorating-payer (warning)     — see SKILL.md PITFALL #1 (currently
                                    a no-op, baseline carry-forward not
                                    ported).
"""

from __future__ import annotations

import json
import os
from typing import Any


def _fmt_aud(n: float) -> str:
    return f"A${n:,.2f}"


def _setting_float(key: str, env_name: str, default: float) -> float:
    raw = os.environ.get("_AR_SETTINGS_JSON")
    if raw:
        try:
            s = json.loads(raw)
            if key in s:
                return float(s[key])
        except Exception:
            pass
    raw_env = os.environ.get(env_name)
    if raw_env:
        try:
            return float(raw_env)
        except ValueError:
            pass
    return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def run_debtor_exposure(entity: str, debtors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag any debtor whose total outstanding crosses the limit. Critical
    regardless of aging — concentration risk is the point.
    """
    limit = _setting_float("debtor_exposure_limit_aud", "AR_DEBTOR_EXPOSURE_LIMIT_AUD", 25_000.0)
    if limit <= 0:
        return []

    findings: list[dict[str, Any]] = []
    for d in debtors:
        if d["totalOutstanding"] < limit:
            continue
        ref = d["contactRef"]
        findings.append({
            "detector": "debtor-exposure-breach",
            "domain": "ar",
            "severity": "critical",
            "entity_code": entity,
            "title": (
                f"{entity}: debtor exposure breach — {ref} "
                f"({_fmt_aud(d['totalOutstanding'])})"
            ),
            "detail": (
                f"Debtor {ref} has total outstanding of "
                f"{_fmt_aud(d['totalOutstanding'])} across "
                f"{d['invoiceCount']} invoice(s), oldest "
                f"{d['oldestAgeDays']} days. Exceeds "
                f"AR_DEBTOR_EXPOSURE_LIMIT_AUD ({_fmt_aud(limit)}). "
                f"Concentration risk — escalate."
            ),
            "amount": d["totalOutstanding"],
            "evidence": {
                "dedupKey": f"debtor-exposure-breach:{entity}:{d['xeroContactId']}",
                "kind": "debtor-exposure-breach",
                "xeroContactId": d["xeroContactId"],
                "contactRef": ref,
                "openInvoiceCount": d["invoiceCount"],
                "oldestAgeDays": d["oldestAgeDays"],
                "limit": limit,
            },
        })
    return findings


def run_deteriorating_payer(entity: str, debtors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """No-op stub — see SKILL.md PITFALL #1.

    The legacy detector compared each debtor's recent vs baseline
    average days-late. Both values live on the legacy Debtor side
    table, which is not ported. Until baseline carry-forward returns,
    this detector emits nothing.
    """
    return []
