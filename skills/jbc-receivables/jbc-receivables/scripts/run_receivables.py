"""jbc-receivables — daily AR detector orchestrator.

Pulls Xero AR snapshots for SC + CQ, ages every outstanding invoice,
aggregates by debtor, then runs 8 detectors (escalation, debtor
exposure, cash application). Writes findings + an audit_runs row into
the shared JBC findings DB.

READ-ONLY on Xero. NEVER sends email. NEVER writes to Xero.

Invocation (typically from `hermes cron`):
    python3 scripts/run_receivables.py
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts import xero_ar                                    # noqa: E402
from scripts import settings as _settings                      # noqa: E402
from scripts.detectors import aging as aging_detectors         # noqa: E402
from scripts.detectors import cash as cash_detectors           # noqa: E402
from scripts.detectors import debtors as debtor_detectors      # noqa: E402

SOURCE_AGENT = "receivables"


# ── DB helpers ────────────────────────────────────────────────────────

def _db_url() -> str:
    url = os.environ.get("JBC_FINDINGS_DATABASE_URL") or os.environ.get(
        "HERMES_FINDINGS_DATABASE_URL"
    )
    if not url:
        raise RuntimeError(
            "JBC_FINDINGS_DATABASE_URL (or HERMES_FINDINGS_DATABASE_URL) must be set"
        )
    return url


def _connect():
    url = _db_url()
    try:
        import psycopg  # v3
        from psycopg.types.json import Jsonb
        conn = psycopg.connect(url)
        return conn, Jsonb
    except ImportError:
        import psycopg2  # v2 fallback
        from psycopg2.extras import Json as Jsonb  # type: ignore
        conn = psycopg2.connect(url)
        return conn, Jsonb


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _insert_audit_run_start(conn, run_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_runs (id, source_agent, run_at, status,
                                    exceptions_count, critical_count,
                                    people_flags_count)
            VALUES (%s, %s, now(), %s, 0, 0, 0)
            """,
            (run_id, SOURCE_AGENT, "running"),
        )
    conn.commit()


def _update_audit_run_end(
    conn, run_id: str, *,
    status: str, total: int, crit: int, people: int,
    duration_ms: int, failure_note: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE audit_runs
               SET status=%s, exceptions_count=%s, critical_count=%s,
                   people_flags_count=%s, duration_ms=%s, failure_note=%s
             WHERE id=%s
            """,
            (status, total, crit, people, duration_ms, failure_note, run_id),
        )
    conn.commit()


def _persist_finding(conn, Jsonb, run_id: str, f: dict[str, Any]) -> bool:
    """Insert one finding, dedup on evidence.dedupKey when present and unresolved."""
    evidence = dict(f.get("evidence") or {})
    evidence.setdefault("runId", run_id)
    evidence.setdefault(
        "runAt",
        _dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    dedup_key = evidence.get("dedupKey")

    with conn.cursor() as cur:
        # Fallback dedup: if no explicit dedupKey, use (source_agent, entity_code, title)
        # so the same structural finding doesn't accumulate across daily runs.
        if not dedup_key:
            _ec = f.get("entity_code", "")
            _ti = f.get("title", "")
            cur.execute(
                """
                SELECT id FROM findings
                 WHERE source_agent=%s
                   AND entity_code=%s
                   AND title=%s
                   AND resolved=false
                 LIMIT 1
                """,
                (SOURCE_AGENT, _ec, _ti),
            )
            _row = cur.fetchone()
            if _row:
                cur.execute(
                    """
                    UPDATE findings
                       SET detail=%s, amount=%s, evidence=%s, run_id=%s
                     WHERE id=%s
                    """,
                    (f.get("detail"), f.get("amount"), Jsonb(evidence), run_id, _row[0]),
                )
                conn.commit()
                return False

        if dedup_key:
            cur.execute(
                """
                SELECT id FROM findings
                 WHERE source_agent=%s
                   AND resolved=false
                   AND evidence->>'dedupKey'=%s
                 LIMIT 1
                """,
                (SOURCE_AGENT, dedup_key),
            )
            row = cur.fetchone()
            if row:
                existing_id = row[0]
                cur.execute(
                    """
                    UPDATE findings
                       SET title=%s, detail=%s, amount=%s, evidence=%s, run_id=%s
                     WHERE id=%s
                    """,
                    (
                        f["title"], f["detail"], f.get("amount"),
                        Jsonb(evidence), run_id, existing_id,
                    ),
                )
                conn.commit()
                return False

        fid = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO findings
                (id, source_agent, run_id, detector, domain, severity,
                 entity_code, is_people_flag, title, detail, amount,
                 ai_explanation, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                fid, SOURCE_AGENT, run_id, f["detector"], f["domain"],
                f["severity"], f["entity_code"], False,
                f["title"], f["detail"], f.get("amount"),
                None, Jsonb(evidence),
            ),
        )
    conn.commit()
    return True


# ── aging + aggregation (pure) ───────────────────────────────────────

def _bucket_of(age_days: int) -> str:
    if age_days <= 0:
        return "current"
    if age_days <= 30:
        return "1-30"
    if age_days <= 60:
        return "31-60"
    if age_days <= 90:
        return "61-90"
    return "90+"


def _status_of(age_days: int, amount_paid: float, amount_due: float,
               writeoff_days: int) -> str:
    if amount_paid > 0 and amount_due > 0:
        return "part-paid"
    if age_days > writeoff_days:
        return "writeoff-candidate"
    return "open"


def age_invoices(
    invoices: list[dict[str, Any]],
    *,
    now: _dt.datetime,
    writeoff_days: int,
) -> list[dict[str, Any]]:
    """Return a list of aged-invoice dicts. Drops invoices with no
    outstanding balance as a belt-and-braces filter."""
    out: list[dict[str, Any]] = []
    for inv in invoices:
        amount_due = float(inv.get("AmountDue") or 0)
        if amount_due <= 0:
            continue
        due = xero_ar.parse_xero_date(inv.get("DueDate")) or \
            xero_ar.parse_xero_date(inv.get("Date")) or now
        issue = xero_ar.parse_xero_date(inv.get("Date")) or due
        age_days = (now.date() - due.date()).days

        contact = inv.get("Contact") or {}
        contact_id = contact.get("ContactID") or ""
        contact_name = contact.get("Name") or "(unknown)"
        amount = float(inv.get("Total") or 0)
        amount_paid = float(inv.get("AmountPaid") or 0)
        out.append({
            "xeroInvoiceId": inv.get("InvoiceID") or "",
            "invoiceNumber": inv.get("InvoiceNumber") or "(no #)",
            "reference": inv.get("Reference"),
            "contactId": contact_id,
            "contactName": contact_name,
            "contactRef": xero_ar.masked_ref(contact_name, contact_id),
            "issueDate": issue.isoformat(),
            "dueDate": due.isoformat(),
            "amount": amount,
            "amountPaid": amount_paid,
            "amountOutstanding": amount_due,
            "ageDays": age_days,
            "ageBucket": _bucket_of(age_days),
            "status": _status_of(age_days, amount_paid, amount_due, writeoff_days),
        })
    return out


def aggregate_debtors(aged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_contact: dict[str, dict[str, Any]] = {}
    for inv in aged:
        cid = inv["contactId"]
        d = by_contact.get(cid)
        if d:
            d["totalOutstanding"] += inv["amountOutstanding"]
            d["oldestAgeDays"] = max(d["oldestAgeDays"], inv["ageDays"])
            d["invoiceCount"] += 1
        else:
            by_contact[cid] = {
                "xeroContactId": cid,
                "name": inv["contactName"],
                "contactRef": inv["contactRef"],
                "totalOutstanding": inv["amountOutstanding"],
                "oldestAgeDays": inv["ageDays"],
                "invoiceCount": 1,
            }
    return sorted(by_contact.values(), key=lambda d: d["totalOutstanding"], reverse=True)


# ── orchestration ────────────────────────────────────────────────────

def _ingest_failure(entity: str, label: str, exc: BaseException) -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": f"{label}-detector-failed",
        "domain": "ingest",
        "severity": "warning",
        "entity_code": entity,
        "title": f"{entity}: receivables {label} raised {type(exc).__name__}",
        "detail": (
            f"Detector failed mid-run: {exc}. Investigate Xero connectivity, "
            f"credentials, or detector logic."
        ),
        "amount": None,
        "evidence": {
            "dedupKey": f"receivables-{label}-failed:{entity}:{today_iso}",
            "kind": "ingest-failure",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=4),
        },
    }


def _run_for_entity(entity: str, *, now: _dt.datetime,
                    writeoff_days: int, payment_lookback_days: int) -> list[dict[str, Any]]:
    """Returns findings for one tenant. Best-effort per phase."""
    if not xero_ar.tenant_configured(entity):
        return []

    findings: list[dict[str, Any]] = []

    # ── pull
    try:
        invoices = xero_ar.list_outstanding_sales_invoices(entity)
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure(entity, "snapshot", exc))
        return findings

    try:
        since_iso = (now - _dt.timedelta(days=payment_lookback_days)).isoformat()
        payments = xero_ar.list_sales_payments_since(entity, since_iso)
    except Exception:  # noqa: BLE001
        payments = []

    # ── age + aggregate
    try:
        aged = age_invoices(invoices, now=now, writeoff_days=writeoff_days)
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure(entity, "aging", exc))
        return findings
    debtors = aggregate_debtors(aged)
    open_invoice_ids = {inv["xeroInvoiceId"] for inv in aged}

    # ── invoice-level detectors
    for label, fn in (
        ("aging-escalation", lambda: aging_detectors.run_aging_escalation(entity, aged)),
        ("writeoff", lambda: aging_detectors.run_writeoff_candidates(entity, aged)),
        ("part-payment", lambda: aging_detectors.run_part_payments(entity, aged)),
        ("disputed", lambda: aging_detectors.run_disputed_invoices(entity, aged)),
    ):
        try:
            findings.extend(fn())
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure(entity, label, exc))

    # ── debtor-level detectors
    for label, fn in (
        ("debtor-exposure", lambda: debtor_detectors.run_debtor_exposure(entity, debtors)),
        ("deteriorating-payer", lambda: debtor_detectors.run_deteriorating_payer(entity, debtors)),
    ):
        try:
            findings.extend(fn())
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure(entity, label, exc))

    # ── cash-application
    try:
        findings.extend(
            cash_detectors.run_unallocated_receipts(
                entity, payments, open_invoice_ids, now=now,
            )
        )
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure(entity, "unallocated-receipt", exc))

    return findings


def _gather_findings() -> list[dict[str, Any]]:
    now = _dt.datetime.now(_dt.timezone.utc)
    # Settings come from the DB (Mark can change them at runtime). The
    # second-tier fallbacks are env vars (legacy) then the original defaults.
    s = _settings.load("receivables")
    writeoff_days = _settings.get_int(s, "writeoff_candidate_days", "AR_WRITEOFF_CANDIDATE_DAYS", 120)
    payment_lookback = _settings.get_int(s, "payment_lookback_days", "AR_PAYMENT_LOOKBACK_DAYS", 180)
    # Stash a snapshot so detectors can read the same keys without re-querying.
    os.environ["_AR_SETTINGS_JSON"] = json.dumps(s)

    findings: list[dict[str, Any]] = []
    for entity in ("SC", "CQ"):
        findings.extend(_run_for_entity(
            entity, now=now,
            writeoff_days=writeoff_days,
            payment_lookback_days=payment_lookback,
        ))

    # Belt-and-braces dedupKey for any finding that forgot.
    today_iso = _dt.date.today().isoformat()
    for f in findings:
        f.setdefault("evidence", {})
        f["evidence"].setdefault("source_agent", SOURCE_AGENT)
        if "dedupKey" not in f["evidence"]:
            f["evidence"]["dedupKey"] = (
                f"{f['detector']}:{f['entity_code']}:{today_iso}"
            )
    return findings


def main() -> int:
    started = time.time()
    run_id = str(uuid.uuid4())

    conn, Jsonb = _connect()
    try:
        _insert_audit_run_start(conn, run_id)

        try:
            findings = _gather_findings()
        except Exception as exc:  # noqa: BLE001
            failure = f"gather_findings crashed: {exc}"
            traceback.print_exc()
            _update_audit_run_end(
                conn, run_id, status="failed", total=0, crit=0, people=0,
                duration_ms=int((time.time() - started) * 1000),
                failure_note=failure,
            )
            return 2

        inserted = 0
        crit = 0
        for f in findings:
            try:
                did_insert = _persist_finding(conn, Jsonb, run_id, f)
                if did_insert:
                    inserted += 1
                if f.get("severity") == "critical":
                    crit += 1
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                print(json.dumps({
                    "warn": "persist-failed", "error": str(exc),
                    "finding": {k: f.get(k) for k in ("detector", "entity_code")},
                }))

        status = "exceptions" if findings else "ok"
        _update_audit_run_end(
            conn, run_id, status=status, total=len(findings), crit=crit, people=0,
            duration_ms=int((time.time() - started) * 1000),
            failure_note=None,
        )

        print(json.dumps({
            "ok": True, "run_id": run_id, "status": status,
            "findings_total": len(findings), "findings_inserted": inserted,
            "findings_deduped": len(findings) - inserted,
            "critical_count": crit,
            "duration_ms": int((time.time() - started) * 1000),
        }, indent=2))
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
