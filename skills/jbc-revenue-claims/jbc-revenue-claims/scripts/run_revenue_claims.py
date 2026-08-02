"""jbc-revenue-claims — daily revenue + claims detector orchestrator.

READ-ONLY. AlayaCare CSV ingest + per-entity Xero AR pull.

Writes findings + an audit_runs row to the shared JBC findings DB.

Env vars: see SKILL.md.
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

from scripts import alayacare_csv                # noqa: E402
from scripts import mark_imports                  # noqa: E402
from scripts import xero_revenue                 # noqa: E402
from scripts.detectors import leakage as leakage_detector   # noqa: E402
from scripts.detectors import pricing as pricing_detector   # noqa: E402
from scripts.detectors import budgets as budgets_detector   # noqa: E402

# Resolve-on-absence lives in the shared lib so all seven runners close findings
# by the same rule. Without it nothing ever leaves the findings table.
if "/data/hermes/lib" not in sys.path:
    sys.path.insert(0, "/data/hermes/lib")
try:
    from findings_sweep import identity_of, resolve_absent  # noqa: E402
    _SWEEP_AVAILABLE = True
except ImportError:  # shared lib not installed — run without sweeping
    _SWEEP_AVAILABLE = False

SOURCE_AGENT = "revenue-claims"

# Detectors whose output is a COMPLETE current-state snapshot each run.
# Only these are eligible for resolve-on-absence; point-in-time event
# detectors are omitted on purpose (see lib/findings_sweep.py).
STATE_DETECTORS = [
    # Detector-failure findings are state too: if the check runs clean
    # next time, the 'this is broken' finding must clear itself.
    "xero-not-configured",
    "ingest-failure",
]



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
        return psycopg.connect(url), Jsonb
    except ImportError:
        import psycopg2  # v2 fallback
        from psycopg2.extras import Json as Jsonb  # type: ignore
        return psycopg2.connect(url), Jsonb


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
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
    conn,
    run_id: str,
    *,
    status: str,
    total: int,
    crit: int,
    people: int,
    duration_ms: int,
    failure_note: str | None,
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
                f["severity"], f["entity_code"], bool(f.get("is_people_flag", False)),
                f["title"], f["detail"], f.get("amount"),
                None, Jsonb(evidence),
            ),
        )
    conn.commit()
    return True


def _ingest_failure(entity: str, label: str, exc: BaseException) -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": f"{label}-detector-failed",
        "domain": "ingest",
        "severity": "warning",
        "entity_code": entity,
        "is_people_flag": False,
        "title": f"{entity}: {label} detector raised {type(exc).__name__}",
        "detail": f"Detector failed mid-run: {exc}. Investigate connectivity and credentials.",
        "amount": None,
        "evidence": {
            "dedupKey": f"{label}-detector-failed:{entity}",
            "kind": "ingest-failure",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=4),
        },
    }


def _alayacare_missing_finding(path: str) -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": "alayacare-export-missing",
        "domain": "ingest",
        "severity": "critical",
        "entity_code": "SC",  # attach to SC by convention; covers both entities
        "is_people_flag": False,
        "title": "AlayaCare delivered-services CSV missing",
        "detail": (
            "ALAYACARE_SERVICE_EXPORT is unset or the file does not exist "
            f"(`{path or '<unset>'}`). Revenue-claims cannot run leakage, "
            "pricing or budget detectors without the daily CSV drop. Until "
            "this is fixed every delivered service is invisible to the "
            "skill — silent revenue leakage risk."
        ),
        "amount": None,
        "evidence": {
            "dedupKey": f"alayacare-export-missing",
            "kind": "alayacare-export-missing",
            "csvSource": path or "",
        },
    }


def _gather_findings() -> tuple[list[dict[str, Any]], bool]:
    """Returns (findings, csv_missing)."""
    findings: list[dict[str, Any]] = []
    window_days = _env_int("REVENUE_CLAIM_WINDOW_DAYS", 60)
    warning_pct = _env_float("REVENUE_BUDGET_WARNING_PCT", 85.0)
    lookback_days = _env_int("REVENUE_LOOKBACK_DAYS", 30)
    papl_version = os.environ.get("NDIS_PAPL_VERSION", "2025-26 v1.1")
    sah_version = os.environ.get("SAH_PRICING_VERSION", "SaH 2025-11 v1")

    # Prefer Mark uploads when configured; fall back to local file otherwise.
    csv_path = mark_imports.fetch_latest_to_tempfile("alayacare")
    if csv_path is None:
        csv_path = os.environ.get("ALAYACARE_SERVICE_EXPORT", "")
    load = alayacare_csv.load(csv_path)
    if load.missing:
        # AlayaCare export is NOT available — Tony confirmed 2 Aug 2026 there
        # are no AlayaCare entries, and the export has never been wired
        # (/data/hermes/imports does not exist). The detectors that need it
        # are therefore opt-in, not broken: skip quietly with the reason on
        # the run log instead of raising every run about a feed that is not
        # coming. Pointing ALAYACARE_* at a real CSV re-enables them with no
        # code change.
        print(
            f"[alayacare] skipped — no export at {csv_path or '<unset>'}; "
            "leakage, pricing and budget detectors not run (opt-in)"
        )
        return findings, True

    services = load.services
    since_iso = (
        _dt.date.today() - _dt.timedelta(days=lookback_days)
    ).isoformat()

    for entity in ("SC", "CQ"):
        # Xero pull (best effort — degrade with ingest finding on failure).
        invoices: list[dict[str, Any]] = []
        if xero_revenue.tenant_configured(entity):
            try:
                invoices = xero_revenue.list_sales_invoices_since(entity, since_iso)
            except Exception as exc:  # noqa: BLE001
                findings.append(_ingest_failure(entity, "xero-ar", exc))
        else:
            today_iso = _dt.date.today().isoformat()
            findings.append({
                "detector": "xero-not-configured",
                "domain": "ingest",
                "severity": "warning",
                "entity_code": entity,
                "is_people_flag": False,
                "title": f"{entity}: Xero credentials not configured",
                "detail": (
                    f"XERO_{entity}_CLIENT_ID/_CLIENT_SECRET/_TENANT_ID not "
                    f"set — leakage cross-check and pricing detectors run "
                    f"without the invoiced side; results will overstate leakage."
                ),
                "amount": None,
                "evidence": {
                    "dedupKey": f"xero-not-configured:{entity}",
                    "kind": "xero-not-configured",
                },
            })

        try:
            findings.extend(leakage_detector.run_leakage(
                entity, services, invoices, window_days=window_days,
            ))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure(entity, "leakage", exc))

        try:
            findings.extend(pricing_detector.run_pricing(
                entity, invoices,
                price_caps=None,  # TODO wire PricingRuleSet config table
                papl_version=papl_version,
                sah_version=sah_version,
            ))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure(entity, "pricing", exc))

        try:
            findings.extend(budgets_detector.run_budgets(
                entity, services,
                budgets_by_participant=None,  # TODO wire ParticipantBudget config
                warning_pct=warning_pct,
            ))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure(entity, "budgets", exc))

    return findings, False


def main() -> int:
    started = time.time()
    run_id = str(uuid.uuid4())

    conn, Jsonb = _connect()
    try:
        _insert_audit_run_start(conn, run_id)

        try:
            findings, csv_missing = _gather_findings()
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
        people = 0
        for f in findings:
            try:
                did_insert = _persist_finding(conn, Jsonb, run_id, f)
                if did_insert:
                    inserted += 1
                if f.get("severity") == "critical":
                    crit += 1
                if f.get("is_people_flag"):
                    people += 1
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                print(json.dumps({"warn": "persist-failed", "error": str(exc),
                                  "finding": {k: f.get(k) for k in ("detector", "entity_code")}}))

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
            conn, run_id, status=status, total=len(findings), crit=crit, people=people,
            duration_ms=int((time.time() - started) * 1000),
            failure_note=("alayacare-export-missing" if csv_missing else None),
        )

        print(json.dumps({
            "ok": True, "run_id": run_id, "status": status,
            "csv_missing": csv_missing,
            "findings_total": len(findings), "findings_inserted": inserted,
            "findings_deduped": len(findings) - inserted,
            "critical_count": crit, "people_flags_count": people,
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
