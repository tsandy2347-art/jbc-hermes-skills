"""Aging / escalation / cash-application detectors over per-invoice rows.

Each `run_*` returns a list of finding-dicts ready for the orchestrator to
persist. `aged_invoices` is the pre-computed per-entity list of aged
invoice dicts (see run_receivables.py::age_invoices).
"""

from __future__ import annotations

import os
from typing import Any


def _fmt_aud(n: float) -> str:
    return f"A${n:,.2f}"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def run_aging_escalation(entity: str, aged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """invoice-90-plus (critical) and invoice-60-plus (warning)."""
    findings: list[dict[str, Any]] = []
    writeoff_days = _env_int("AR_WRITEOFF_CANDIDATE_DAYS", 120)
    for inv in aged:
        bucket = inv["ageBucket"]
        age = inv["ageDays"]
        if bucket == "90+":
            detector = "invoice-90-plus"
            severity = "critical"
            band = "over 90"
        elif bucket == "61-90":
            detector = "invoice-60-plus"
            severity = "warning"
            band = "61–90"
        else:
            continue
        ref = inv["contactRef"]
        findings.append({
            "detector": detector,
            "domain": "ar",
            "severity": severity,
            "entity_code": entity,
            "title": f"{entity}: invoice {inv['invoiceNumber']} {band} days overdue — {ref}",
            "detail": (
                f"Invoice {inv['invoiceNumber']} for {ref} is {age} days past due. "
                f"Outstanding {_fmt_aud(inv['amountOutstanding'])}. "
                f"Issued {inv['issueDate'][:10]}, due {inv['dueDate'][:10]}. "
                f"Hand to collections."
            ),
            "amount": inv["amountOutstanding"],
            "evidence": {
                "dedupKey": f"{detector}:{entity}:{inv['xeroInvoiceId']}",
                "kind": detector,
                "xeroInvoiceId": inv["xeroInvoiceId"],
                "xeroContactId": inv["contactId"],
                "invoiceNumber": inv["invoiceNumber"],
                "contactRef": ref,
                "ageDays": age,
                "ageBucket": bucket,
                "writeoffThresholdDays": writeoff_days,
            },
        })
    return findings


def run_writeoff_candidates(entity: str, aged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """writeoff-candidate (critical) — invoice older than threshold."""
    threshold = _env_int("AR_WRITEOFF_CANDIDATE_DAYS", 120)
    findings: list[dict[str, Any]] = []
    for inv in aged:
        if inv["ageDays"] < threshold:
            continue
        ref = inv["contactRef"]
        findings.append({
            "detector": "writeoff-candidate",
            "domain": "ar",
            "severity": "critical",
            "entity_code": entity,
            "title": (
                f"{entity}: write-off CANDIDATE — invoice {inv['invoiceNumber']} "
                f"({inv['ageDays']} days, {ref})"
            ),
            "detail": (
                f"Invoice {inv['invoiceNumber']} for {ref} is {inv['ageDays']} days past due "
                f"(threshold {threshold}). Outstanding {_fmt_aud(inv['amountOutstanding'])}. "
                f"CANDIDATE ONLY — the skill never writes anything off. "
                f"A named human must (1) review, (2) action the write-off in Xero, "
                f"(3) resolve this finding with a note describing what they did."
            ),
            "amount": inv["amountOutstanding"],
            "evidence": {
                "dedupKey": f"writeoff-candidate:{entity}:{inv['xeroInvoiceId']}",
                "kind": "writeoff-candidate",
                "xeroInvoiceId": inv["xeroInvoiceId"],
                "xeroContactId": inv["contactId"],
                "invoiceNumber": inv["invoiceNumber"],
                "contactRef": ref,
                "ageDays": inv["ageDays"],
                "thresholdDays": threshold,
            },
        })
    return findings


def run_part_payments(entity: str, aged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """part-payment (warning) — AmountPaid > 0 AND AmountDue > 0."""
    findings: list[dict[str, Any]] = []
    for inv in aged:
        if inv["status"] != "part-paid":
            continue
        ref = inv["contactRef"]
        findings.append({
            "detector": "part-payment",
            "domain": "ar",
            "severity": "warning",
            "entity_code": entity,
            "title": f"{entity}: part-payment — invoice {inv['invoiceNumber']} ({ref})",
            "detail": (
                f"Invoice {inv['invoiceNumber']} for {ref} has been part-paid. "
                f"Paid {_fmt_aud(inv['amountPaid'])} of {_fmt_aud(inv['amount'])}, "
                f"still outstanding {_fmt_aud(inv['amountOutstanding'])}. "
                f"Confirm whether this is a planned instalment or a short-payment to chase."
            ),
            "amount": inv["amountOutstanding"],
            "evidence": {
                "dedupKey": f"part-payment:{entity}:{inv['xeroInvoiceId']}",
                "kind": "part-payment",
                "xeroInvoiceId": inv["xeroInvoiceId"],
                "xeroContactId": inv["contactId"],
                "invoiceNumber": inv["invoiceNumber"],
                "contactRef": ref,
                "amount": inv["amount"],
                "amountPaid": inv["amountPaid"],
                "amountOutstanding": inv["amountOutstanding"],
            },
        })
    return findings


def run_disputed_invoices(entity: str, aged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """disputed-invoice (warning).

    Xero has no native dispute flag. Legacy agent read this from an internal
    DB column populated through the /exceptions UI. The skill does NOT
    carry that side table, so this detector ships as a no-op stub. It's
    here so the detector signature is wired and can be enabled the moment a
    dispute-marking surface returns (e.g. via Mark resolution-note
    convention).
    """
    return []
