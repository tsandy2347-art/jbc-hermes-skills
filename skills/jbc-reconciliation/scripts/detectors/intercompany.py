"""Domain B — intercompany loan reconciliation.

Computed once across BOTH tenants. All findings are attached to SC
(`entity_code='SC'`) per the legacy quirk — single source of truth, no
duplicate display on Mark's dashboard.

Emits:
  intercompany-codes-not-configured   (critical)
  intercompany-balance-unreadable     (critical)
  intercompany-mismatch               (critical)
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from ..xero_client import trial_balance


def _fmt_aud(n: float) -> str:
    return f"A${n:,.2f}"


def _find_code(rows: list[dict[str, Any]], code: str) -> float | None:
    for row in rows or []:
        if row.get("Rows"):
            inner = _find_code(row["Rows"], code)
            if inner is not None:
                return inner
        if row.get("RowType") == "Row":
            cells = row.get("Cells") or []
            if not cells:
                continue
            first = cells[0]
            attrs = first.get("Attributes") or []
            by_attr = any(
                a.get("Id") in ("account", "code") and a.get("Value") == code
                for a in attrs
            )
            text = str(first.get("Value", ""))
            by_text = text.startswith(f"{code} ") or text.startswith(f"{code}-")
            if by_attr or by_text:
                numerics: list[float] = []
                for i in range(1, len(cells)):
                    v = cells[i].get("Value", "")
                    try:
                        numerics.append(float(str(v).replace(",", "").replace(" ", "")))
                    except (ValueError, TypeError):
                        continue
                if not numerics:
                    return 0.0
                if len(numerics) >= 2:
                    return numerics[0] - numerics[1]
                return numerics[0]
    return None


def _extract(report: dict[str, Any] | None, code: str) -> float | None:
    if not report:
        return None
    reports = report.get("Reports") or []
    if not reports:
        return None
    return _find_code(reports[0].get("Rows") or [], code)


def run_intercompany(*, tolerance_aud: float) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    today = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=10))
    )
    today_iso = today.date().isoformat()

    sc_code = os.environ.get("XERO_SC_LOAN_TO_CQ_CODE", "").strip()
    cq_code = os.environ.get("XERO_CQ_LOAN_FROM_SC_CODE", "").strip()

    if not sc_code or not cq_code:
        findings.append({
            "detector": "intercompany-codes-not-configured",
            "domain": "intercompany",
            "severity": "critical",
            "entity_code": "SC",
            "title": "Intercompany account codes not configured",
            "detail": "XERO_SC_LOAN_TO_CQ_CODE and/or XERO_CQ_LOAN_FROM_SC_CODE are unset. "
                      "Intercompany reconciliation is SKIPPED until codes are configured.",
            "amount": None,
            "evidence": {
                "dedupKey": "intercompany-codes-not-configured",
                "kind": "intercompany-codes-not-configured",
                "scCode": sc_code or None, "cqCode": cq_code or None,
            },
        })
        return findings

    try:
        sc_tb = trial_balance("SC", today_iso)
    except Exception:
        sc_tb = None
    try:
        cq_tb = trial_balance("CQ", today_iso)
    except Exception:
        cq_tb = None

    sc_balance = _extract(sc_tb, sc_code)
    cq_balance = _extract(cq_tb, cq_code)

    if sc_balance is None or cq_balance is None:
        findings.append({
            "detector": "intercompany-balance-unreadable",
            "domain": "intercompany",
            "severity": "critical",
            "entity_code": "SC",
            "title": "Intercompany account balance not readable",
            "detail": (
                f"Could not extract balance for SC code {sc_code} "
                f"({'missing' if sc_balance is None else 'ok'}) or "
                f"CQ code {cq_code} ({'missing' if cq_balance is None else 'ok'}) "
                "from the Trial Balance reports. Check the codes exist and are active in both tenants."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": "intercompany-balance-unreadable",
                "kind": "intercompany-balance-unreadable",
                "scCode": sc_code, "cqCode": cq_code,
                "scBalance": sc_balance, "cqBalance": cq_balance,
            },
        })
        return findings

    difference = abs(abs(sc_balance) - abs(cq_balance))
    if difference > tolerance_aud:
        findings.append({
            "detector": "intercompany-mismatch",
            "domain": "intercompany",
            "severity": "critical",
            "entity_code": "SC",
            "title": f"Intercompany mismatch: {_fmt_aud(difference)} apart",
            "detail": (
                f"SC \"Loan to CQ\" (code {sc_code}) shows {_fmt_aud(sc_balance)}. "
                f"CQ \"Loan from SC\" (code {cq_code}) shows {_fmt_aud(cq_balance)}. "
                f"Gap of {_fmt_aud(difference)} exceeds tolerance {_fmt_aud(tolerance_aud)}. "
                "Identify the transactions that exist on one side but not the other."
            ),
            "amount": difference,
            "evidence": {
                # Daily dedupKey — keeps a row per day so the trend is visible
                # rather than a single perpetually-open finding.
                "dedupKey": f"intercompany-mismatch:{today_iso}",
                "kind": "intercompany-mismatch",
                "scCode": sc_code, "cqCode": cq_code,
                "scBalance": sc_balance, "cqBalance": cq_balance,
                "difference": difference, "toleranceAud": tolerance_aud,
            },
        })

    return findings
