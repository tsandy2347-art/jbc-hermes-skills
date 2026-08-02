"""jbc-payroll-labour — payroll/labour analyser orchestrator.

READ-ONLY. Ingests MYOB + AlayaCare CSV exports, runs ten detectors,
writes findings + an audit_runs row.

Invocation:
    python3 scripts/run_payroll_labour.py
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

from scripts import myob_csv, alayacare_csv, schads_engine, mark_imports  # noqa: E402
from scripts.detectors import award as award_det  # noqa: E402
from scripts.detectors import integrity as integrity_det  # noqa: E402
from scripts.detectors import labour as labour_det  # noqa: E402
from scripts.detectors import rostering as rostering_det  # noqa: E402

# Resolve-on-absence lives in the shared lib so all seven runners close findings
# by the same rule. Without it nothing ever leaves the findings table.
if "/data/hermes/lib" not in sys.path:
    sys.path.insert(0, "/data/hermes/lib")
try:
    from findings_sweep import identity_of, resolve_absent  # noqa: E402
    _SWEEP_AVAILABLE = True
except ImportError:  # shared lib not installed — run without sweeping
    _SWEEP_AVAILABLE = False

SOURCE_AGENT = "payroll-labour"

# Detectors whose output is a COMPLETE current-state snapshot each run.
# Only these are eligible for resolve-on-absence; point-in-time event
# detectors are omitted on purpose (see lib/findings_sweep.py).
STATE_DETECTORS = [
    "labour-cost-pct",
    "unverified-line",
    "duplicate-payline",
    # Detector-failure findings are state too: if the check runs clean
    # next time, the 'this is broken' finding must clear itself.
    "myob-export-missing",
    "myob-export-unreadable",
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
        import psycopg2  # type: ignore
        from psycopg2.extras import Json as Jsonb  # type: ignore
        return psycopg2.connect(url), Jsonb


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
        "runAt", _dt.datetime.now(_dt.timezone.utc).isoformat()
    )
    dedup_key = evidence.get("dedupKey")
    is_people = bool(f.get("is_people_flag", False))

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
                cur.execute(
                    """
                    UPDATE findings
                       SET title=%s, detail=%s, amount=%s, evidence=%s, run_id=%s,
                           severity=%s, is_people_flag=%s
                     WHERE id=%s
                    """,
                    (
                        f["title"], f["detail"], f.get("amount"),
                        Jsonb(evidence), run_id, f["severity"], is_people,
                        row[0],
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
                f["severity"], f["entity_code"], is_people,
                f["title"], f["detail"], f.get("amount"),
                None, Jsonb(evidence),
            ),
        )
    conn.commit()
    return True


def _ingest_failure(detector: str, label: str, exc: BaseException,
                    entity: str = "SC") -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": detector,
        "domain": "ingest",
        "severity": "warning",
        "entity_code": entity,
        "is_people_flag": False,
        "title": f"{label} raised {type(exc).__name__}",
        "detail": f"{label} failed mid-run: {exc}. Investigate inputs.",
        "amount": None,
        "evidence": {
            "dedupKey": f"{detector}",
            "kind": "ingest-failure",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=4),
        },
    }


def _gather_findings() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    today_iso = _dt.date.today().isoformat()

    tol = _env_float("PAY_VARIANCE_TOLERANCE_AUD", 1.0)
    systemic_min = _env_int("SYSTEMIC_MIN_STAFF_AFFECTED", 3)
    ot_pct = _env_float("OVERTIME_SPEND_ALERT_PCT", 5.0)
    util_floor = _env_float("UTILISATION_FLOOR_PCT", 85.0)
    target_sc = _env_float("LABOUR_COST_TARGET_PCT_SC", 70.0)
    target_cq = _env_float("LABOUR_COST_TARGET_PCT_CQ", 70.0)

    # --- MYOB ingest ---
    # Prefer Mark uploads when configured; fall back to local file otherwise.
    myob_path = mark_imports.fetch_latest_to_tempfile("myob")
    if myob_path is None:
        myob_path = os.environ.get("MYOB_EXPORT_PATH",
                                    "/data/hermes/imports/myob_latest.csv")
    myob = myob_csv.load(myob_path)
    if myob.missing:
        findings.append({
            "detector": "myob-export-missing",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": "SC",
            "is_people_flag": False,
            "title": "MYOB Pay Activity Summary CSV missing",
            "detail": (
                f"Expected MYOB CSV at {myob.path}. Detectors that depend on "
                "MYOB will be skipped this run. Drop the export and rerun."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"myob-export-missing",
                "expectedPath": myob.path,
            },
        })
    elif myob.error:
        # Fail loud: a present-but-unparseable export must never read as 0 rows
        # / all-clear. Surface it so the run isn't a false green.
        findings.append({
            "detector": "myob-export-unreadable",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": "SC",
            "is_people_flag": False,
            "title": "MYOB Pay Activity export could not be parsed",
            "detail": (
                f"Export at {myob.path} could not be read ({myob.error}). "
                "MYOB-dependent detectors were skipped this run."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"myob-export-unreadable",
                "path": myob.path,
                "error": myob.error,
            },
        })

    # --- AlayaCare ingest ---
    # Prefer Mark uploads when configured; fall back to local file otherwise.
    ac_path = mark_imports.fetch_latest_to_tempfile("alayacare")
    if ac_path is None:
        ac_path = os.environ.get("ALAYACARE_EXPORT_PATH",
                                 "/data/hermes/imports/alayacare_latest.csv")
    ac = alayacare_csv.load(ac_path)
    if ac.missing:
        findings.append({
            "detector": "alayacare-export-missing",
            "domain": "ingest",
            "severity": "info",
            "entity_code": "SC",
            "is_people_flag": False,
            "title": "AlayaCare timesheet CSV missing",
            "detail": (
                f"Expected AlayaCare CSV at {ac.path}. Hourly-shape detectors "
                "(ghost-shift, overtime-spike, utilisation-drop, "
                "broken-shift-trigger) will be skipped this run."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"alayacare-export-missing",
                "expectedPath": ac.path,
            },
        })

    if not myob.lines:
        return findings  # nothing to analyse beyond ingest signals

    # --- SCHADS recompute ---
    ruleset_path = os.environ.get("SCHADS_RULESET_PATH")
    ruleset = schads_engine.load_ruleset(ruleset_path) if ruleset_path else None
    line_checks = schads_engine.recompute(myob.lines, ruleset)

    # --- Detectors ---
    try:
        findings.extend(award_det.detect_pay_line_variance(line_checks, tol))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("pay-line-variance-failed",
                                        "pay-line-variance detector", exc))
    try:
        findings.extend(award_det.detect_unverified_line(line_checks))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("unverified-line-failed",
                                        "unverified-line detector", exc))
    try:
        findings.extend(award_det.detect_systemic_underpayment(
            line_checks, systemic_min))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("systemic-underpayment-failed",
                                        "systemic-underpayment detector", exc))

    try:
        findings.extend(integrity_det.detect_super_miscalc(
            myob.lines, ruleset, tol))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("super-miscalc-failed",
                                        "super-miscalc detector", exc))
    try:
        findings.extend(integrity_det.detect_duplicate_payline(myob.lines))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("duplicate-payline-failed",
                                        "duplicate-payline detector", exc))
    if ac.shifts:
        try:
            findings.extend(integrity_det.detect_ghost_shift(
                myob.lines, ac.shifts))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure("ghost-shift-failed",
                                            "ghost-shift detector", exc))

    # Labour analytics.
    revenue = {
        "SC": _env_float("SC_REVENUE_AUD", 0.0) or None,
        "CQ": _env_float("CQ_REVENUE_AUD", 0.0) or None,
    }
    try:
        findings.extend(labour_det.detect_labour_cost_pct(
            myob.lines, revenue, {"SC": target_sc, "CQ": target_cq}))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("labour-cost-pct-failed",
                                        "labour-cost-pct detector", exc))
    if ac.shifts:
        try:
            findings.extend(labour_det.detect_utilisation_drop(
                ac.shifts, util_floor))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure("utilisation-drop-failed",
                                            "utilisation-drop detector", exc))
    try:
        findings.extend(labour_det.detect_overtime_spike(myob.lines, ot_pct))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("overtime-spike-failed",
                                        "overtime-spike detector", exc))

    if ac.shifts:
        try:
            findings.extend(rostering_det.detect_broken_shift_trigger(ac.shifts))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure("broken-shift-trigger-failed",
                                            "broken-shift-trigger detector", exc))

    # Belt-and-braces dedupKey + source_agent annotation.
    for f in findings:
        ev = f.setdefault("evidence", {})
        ev.setdefault("source_agent", SOURCE_AGENT)
        if "dedupKey" not in ev:
            ev["dedupKey"] = f"{f['detector']}:{f.get('entity_code','SC')}"

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
            traceback.print_exc()
            _update_audit_run_end(
                conn, run_id, status="failed", total=0, crit=0, people=0,
                duration_ms=int((time.time() - started) * 1000),
                failure_note=f"gather_findings crashed: {exc}",
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
            conn, run_id, status=status, total=len(findings),
            crit=crit, people=people,
            duration_ms=int((time.time() - started) * 1000),
            failure_note=None,
        )

        print(json.dumps({
            "ok": True, "run_id": run_id, "status": status,
            "findings_total": len(findings),
            "findings_inserted": inserted,
            "findings_deduped": len(findings) - inserted,
            "critical_count": crit, "people_flag_count": people,
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
