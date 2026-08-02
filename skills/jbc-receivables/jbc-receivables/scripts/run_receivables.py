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

# Resolve-on-absence lives in the shared lib so all seven runners close findings
# by the same rule. Without it nothing ever leaves the findings table.
if "/data/hermes/lib" not in sys.path:
    sys.path.insert(0, "/data/hermes/lib")
try:
    from findings_sweep import identity_of, resolve_absent  # noqa: E402
    _SWEEP_AVAILABLE = True
except ImportError:  # shared lib not installed — run without sweeping
    _SWEEP_AVAILABLE = False

SOURCE_AGENT = "receivables"

# Detectors whose output is a COMPLETE current-state snapshot each run.
# Only these are eligible for resolve-on-absence; point-in-time event
# detectors are omitted on purpose (see lib/findings_sweep.py).
STATE_DETECTORS = [
    "invoice-60-plus",
    "invoice-90-plus",
    "writeoff-candidate",
    "part-payment",
    "ar-total",
    "ar-aging-buckets",
    "ar-collections-weekly",
    "debtor-exposure-breach",
]



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
            "dedupKey": f"receivables-{label}-failed:{entity}",
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

    # ── ar-aging-buckets detector
    # Writes one finding per entity per run with bucket totals.
    # 61-90d and 90+d are critical — target is ZERO for both.
    if aged:
        bkt = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
        cnt = {"current": 0,   "1-30": 0,   "31-60": 0,   "61-90": 0,   "90+": 0}
        for inv in aged:
            b = inv.get("ageBucket", "current")
            bkt[b] = bkt.get(b, 0.0) + inv["amountOutstanding"]
            cnt[b] = cnt.get(b, 0) + 1
        today_iso = _dt.date.today().isoformat()
        d61_90  = bkt["61-90"]
        d90plus = bkt["90+"]
        sev = "critical" if (d61_90 > 0 or d90plus > 0) else "info"
        findings.append({
            "detector": "ar-aging-buckets",
            "domain": "aging",
            "severity": sev,
            "entity_code": entity,
            "title": (
                f"{entity} AR aging: 61-90d ${d61_90:,.0f}  90+d ${d90plus:,.0f}  "
                f"{'⚠️ action required' if sev == 'critical' else '✓ clean'}"
            ),
            "detail": (
                f"{entity} AR aging as at {today_iso}: "
                f"Current ${bkt['current']:,.2f} ({cnt['current']}), "
                f"1-30d ${bkt['1-30']:,.2f} ({cnt['1-30']}), "
                f"31-60d ${bkt['31-60']:,.2f} ({cnt['31-60']}), "
                f"61-90d ${d61_90:,.2f} ({cnt['61-90']}), "
                f"90+d ${d90plus:,.2f} ({cnt['90+']}). "
                f"Target: 61-90d and 90+d = $0. "
                f"{'Immediate follow-up and write-off required.' if sev == 'critical' else 'All overdue buckets clear.'}"
            ),
            "amount": round(d61_90 + d90plus, 2),
            "evidence": {
                "dedupKey":      f"ar-aging-buckets:{entity}",
                "current":       round(bkt["current"], 2),
                "d1to30":        round(bkt["1-30"], 2),
                "d31to60":       round(bkt["31-60"], 2),
                "d61to90":       round(d61_90, 2),
                "d90plus":       round(d90plus, 2),
                "count_current": cnt["current"],
                "count_1to30":   cnt["1-30"],
                "count_31to60":  cnt["31-60"],
                "count_61to90":  cnt["61-90"],
                "count_90plus":  cnt["90+"],
                "asOf":          today_iso,
            },
        })

    # ── ar-collections-weekly detector
    # Sums payments received vs new invoices raised in last 7 days.
    try:
        seven_days_ago = (now - _dt.timedelta(days=7)).isoformat()
        weekly_payments = xero_ar.list_sales_payments_since(entity, seven_days_ago)
        cash_collected = sum(float(p.get("Amount") or 0) for p in weekly_payments)
        new_invoice_amount = sum(
            float(inv.get("Total") or 0)
            for inv in invoices
            if (xero_ar.parse_xero_date(inv.get("Date")) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
               >= (now - _dt.timedelta(days=7))
        )
        today_iso = _dt.date.today().isoformat()
        findings.append({
            "detector": "ar-collections-weekly",
            "domain": "intelligence",
            "severity": "info",
            "entity_code": entity,
            "title": (
                f"{entity} weekly: collected ${cash_collected:,.0f}, "
                f"new billed ${new_invoice_amount:,.0f}"
            ),
            "detail": (
                f"{entity} last 7 days: collected ${cash_collected:,.2f} from "
                f"{len(weekly_payments)} payments. New invoices: "
                f"${new_invoice_amount:,.2f}. "
                f"{'AR reducing.' if cash_collected >= new_invoice_amount else 'AR growing.'}"
            ),
            "amount": round(cash_collected, 2),
            "evidence": {
                "dedupKey":          f"ar-collections-weekly:{entity}",
                "cashCollected":     round(cash_collected, 2),
                "paymentCount":      len(weekly_payments),
                "newInvoicedAmount": round(new_invoice_amount, 2),
                "asOf":              today_iso,
            },
        })
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure(entity, "ar-collections-weekly", exc))

    # ── ar-total summary detector
    # One info finding per entity per run — history for week-over-week tracking.
    if aged:
        total_outstanding = sum(inv["amountOutstanding"] for inv in aged)
        total_invoices = len(aged)
        today_iso = _dt.date.today().isoformat()
        findings.append({
            "detector": "ar-total",
            "domain": "intelligence",
            "severity": "info",
            "entity_code": entity,
            "title": f"{entity} total AR: ${total_outstanding:,.2f} ({total_invoices} invoices)",
            "detail": (
                f"Total AR outstanding for {entity} as at {today_iso}: "
                f"${total_outstanding:,.2f} across {total_invoices} invoices."
            ),
            "amount": round(total_outstanding, 2),
            "evidence": {
                "dedupKey":      f"ar-total:{entity}",
                "totalOutstanding": round(total_outstanding, 2),
                "invoiceCount":  total_invoices,
                "asOf":          today_iso,
            },
        })

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

        # Close what this run no longer sees. A run carrying any ingest failure
        # sweeps nothing — see lib/findings_sweep.py for why that matters.
        had_failures = any(
            f.get("detector") == "ingest-failure" or f.get("domain") == "ingest"
            for f in findings
        )
        sweep = {"swept": False, "reason": "findings_sweep unavailable", "resolved": 0}
        if _SWEEP_AVAILABLE:
            try:
                sweep = resolve_absent(
                    conn,
                    source_agent=SOURCE_AGENT,
                    run_id=run_id,
                    emitted=[identity_of(SOURCE_AGENT, f) for f in findings],
                    had_failures=had_failures,
                    state_detectors=STATE_DETECTORS,
                )
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                sweep = {"swept": False, "reason": f"sweep crashed: {exc}", "resolved": 0}
        print(json.dumps({"sweep": sweep}))

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
