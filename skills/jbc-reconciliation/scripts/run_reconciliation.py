"""jbc-reconciliation — daily detector orchestrator.

Pulls Xero snapshots for SC + CQ, runs the three detector domains, writes
findings + an audit_runs row into the shared JBC findings DB.

READ-ONLY on Xero. No writes whatsoever.

Invocation (typically from `hermes cron`):
    python3 scripts/run_reconciliation.py

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

# Allow `python3 scripts/run_reconciliation.py` as well as `python3 -m`.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts.detectors import bank as bank_detector       # noqa: E402
from scripts.detectors import intercompany as ic_detector # noqa: E402
from scripts.detectors import journal as journal_detector # noqa: E402

SOURCE_AGENT = "reconciliation"


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
    """Return (conn, Json) where Json wraps a dict to a jsonb-safe value."""
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


def _insert_audit_run_start(conn, Jsonb, run_id: str) -> None:
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
    """Insert one finding, dedup on evidence.dedupKey when present and unresolved.

    Returns True if a row was inserted, False if deduped (updated in place).
    """
    evidence = dict(f.get("evidence") or {})
    evidence.setdefault("runId", run_id)
    evidence.setdefault(
        "runAt",
        _dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    dedup_key = evidence.get("dedupKey")

    with conn.cursor() as cur:
        if dedup_key:
            # Check for an existing UNRESOLVED row with the same dedupKey + source_agent.
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


def _gather_findings() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    lookback = _env_int("RECON_LOOKBACK_DAYS", 90)
    unmatched = _env_int("RECON_UNMATCHED_DAYS", 2)
    lag = _env_int("RECON_JOURNAL_LAG_DAYS", 3)
    large = _env_float("RECON_LARGE_JOURNAL_AUD", 10_000.0)
    tolerance = _env_float("RECON_INTERCOMPANY_TOLERANCE_AUD", 1.0)

    # Per-entity bank + journal.
    for entity in ("SC", "CQ"):
        try:
            findings.extend(bank_detector.run_bank(
                entity, lookback_days=lookback, unmatched_days=unmatched,
            ))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure(entity, "bank", exc))
        try:
            findings.extend(journal_detector.run_journal(
                entity, lag_days=lag, large_threshold_aud=large,
            ))
        except Exception as exc:  # noqa: BLE001
            findings.append(_ingest_failure(entity, "journal", exc))

    # Intercompany once across both, attached to SC.
    try:
        findings.extend(ic_detector.run_intercompany(tolerance_aud=tolerance))
    except Exception as exc:  # noqa: BLE001
        findings.append(_ingest_failure("SC", "intercompany", exc))

    # Enforce invariants from SKILL.md hard rules.
    today_iso = _dt.date.today().isoformat()
    for f in findings:
        f.setdefault("evidence", {})
        # is_people_flag is hardcoded false at persist time, but normalise here too.
        f["evidence"].setdefault("source_agent", SOURCE_AGENT)
        # Belt-and-braces dedupKey if a detector forgot.
        if "dedupKey" not in f["evidence"]:
            f["evidence"]["dedupKey"] = (
                f"{f['detector']}:{f['entity_code']}:{today_iso}"
            )

    return findings


def _ingest_failure(entity: str, label: str, exc: BaseException) -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": f"{label}-detector-failed",
        "domain": "ingest",
        "severity": "warning",
        "entity_code": entity,
        "title": f"{entity}: {label} detector raised {type(exc).__name__}",
        "detail": f"Detector failed mid-run: {exc}. Investigate Xero connectivity and credentials.",
        "amount": None,
        "evidence": {
            "dedupKey": f"{label}-detector-failed:{entity}:{today_iso}",
            "kind": "ingest-failure",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=4),
        },
    }


def main() -> int:
    started = time.time()
    run_id = str(uuid.uuid4())

    conn, Jsonb = _connect()
    try:
        _insert_audit_run_start(conn, Jsonb, run_id)

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
                # Best-effort — don't let a single bad row kill the whole run.
                print(json.dumps({"warn": "persist-failed", "error": str(exc),
                                  "finding": {k: f.get(k) for k in ("detector", "entity_code")}}))

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
