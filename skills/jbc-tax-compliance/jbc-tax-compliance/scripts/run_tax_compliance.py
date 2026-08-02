"""jbc-tax-compliance — daily detector orchestrator.

READ-ONLY on Xero. NEVER lodges with the ATO / QRO. Writes findings +
audit_runs to the shared JBC findings DB only.

Per-entity isolation is strict: SC and CQ are separate taxpayers and the
skill never emits a consolidated finding except `entity_code='GROUPED'`
on `payroll-tax-threshold` when TAX_PAYROLL_TAX_GROUPED=true.

Invocation:
    python3 scripts/run_tax_compliance.py

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

from scripts.detectors import gst as gst_detector            # noqa: E402
from scripts.detectors import bas as bas_detector            # noqa: E402
from scripts.detectors import payg as payg_detector          # noqa: E402
from scripts.detectors import super_ as super_detector       # noqa: E402
from scripts.detectors import payroll_tax as payroll_tax_detector  # noqa: E402
from scripts.jbc_tax_rulesets import DEFAULT_LOOKBACK_DAYS   # noqa: E402

# Resolve-on-absence lives in the shared lib so all seven runners close findings
# by the same rule. Without it nothing ever leaves the findings table.
if "/data/hermes/lib" not in sys.path:
    sys.path.insert(0, "/data/hermes/lib")
try:
    from findings_sweep import identity_of, resolve_absent  # noqa: E402
    _SWEEP_AVAILABLE = True
except ImportError:  # shared lib not installed — run without sweeping
    _SWEEP_AVAILABLE = False

SOURCE_AGENT = "tax-compliance"

# Detectors whose output is a COMPLETE current-state snapshot each run.
# Only these are eligible for resolve-on-absence; point-in-time event
# detectors are omitted on purpose (see lib/findings_sweep.py).
STATE_DETECTORS = [
    "gst-position",
    "payg-clearing-position",
    "super-clearing-position",
    "payroll-tax-threshold",
    "bas-deadline",
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
        import psycopg
        from psycopg.types.json import Jsonb
        conn = psycopg.connect(url)
        return conn, Jsonb
    except ImportError:
        import psycopg2
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
    conn, run_id: str, *, status: str, total: int, crit: int, people: int,
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
    evidence = dict(f.get("evidence") or {})
    evidence.setdefault("runId", run_id)
    evidence.setdefault(
        "runAt",
        _dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    dedup_key = evidence.get("dedupKey")

    # Enforce SCHEMA §6: consolidated forbidden on statutory domains.
    if f.get("entity_code") == "consolidated":
        raise ValueError(
            "tax-compliance MUST NOT emit consolidated findings — "
            "use 'SC', 'CQ', or 'GROUPED' (payroll-tax only)."
        )

    with conn.cursor() as cur:
        if not dedup_key:
            _ec = f.get("entity_code", "")
            _ti = f.get("title", "")
            cur.execute(
                """
                SELECT id FROM findings
                 WHERE source_agent=%s AND entity_code=%s AND title=%s AND resolved=false LIMIT 1
                """,
                (SOURCE_AGENT, _ec, _ti),
            )
            _row = cur.fetchone()
            if _row:
                cur.execute(
                    """UPDATE findings SET detail=%s, amount=%s, evidence=%s, run_id=%s WHERE id=%s""",
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
                       SET title=%s, detail=%s, amount=%s, evidence=%s,
                           run_id=%s, severity=%s
                     WHERE id=%s
                    """,
                    (
                        f["title"], f["detail"], f.get("amount"),
                        Jsonb(evidence), run_id, f["severity"], existing_id,
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


def _ingest_failure(entity: str, label: str, exc: BaseException) -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": f"{label}-detector-failed",
        "domain": "ingest",
        "severity": "warning",
        "entity_code": entity,
        "title": f"{entity}: tax-{label} detector raised {type(exc).__name__}",
        "detail": (
            f"Detector failed mid-run: {exc}. Investigate Xero connectivity "
            f"and credentials."
        ),
        "amount": None,
        "evidence": {
            "dedupKey": f"{label}-detector-failed:{entity}",
            "kind": "ingest-failure",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=4),
        },
    }


def _gather_findings() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    lookback = _env_int("TAX_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)

    # Per-entity domains: gst, bas, payg, super.
    for entity in ("SC", "CQ"):
        for label, fn in (
            ("gst", lambda e=entity: gst_detector.run_gst(e, lookback_days=lookback)),
            ("bas", lambda e=entity: bas_detector.run_bas(e)),
            ("payg", lambda e=entity: payg_detector.run_payg(e)),
            ("super", lambda e=entity: super_detector.run_super(e)),
        ):
            try:
                findings.extend(fn())
            except Exception as exc:  # noqa: BLE001
                findings.append(_ingest_failure(entity, label, exc))

    # Payroll-tax runs once across both entities (handles grouping internally).
    try:
        findings.extend(payroll_tax_detector.run_payroll_tax())
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("SC", "payroll-tax", exc))

    # Normalise.
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
            "findings_total": len(findings),
            "findings_inserted": inserted,
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
