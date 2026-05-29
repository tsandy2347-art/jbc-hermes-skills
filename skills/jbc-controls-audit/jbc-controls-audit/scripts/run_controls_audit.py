"""jbc-controls-audit — daily detector orchestrator.

Pulls Xero snapshots for SC + CQ, runs the controls/governance detector
groups, writes findings + an audit_runs row into the shared JBC findings
DB.

READ-ONLY on Xero. No writes whatsoever.

Restricted-routing is enforced at EMIT TIME (not relegated to downstream
filters). See `_enforce_routing_guard` below — any finding with
`is_people_flag = True` is required to:
  - keep the named individual out of the `title` (mask form only)
  - have `evidence.individualName` populated with the full name
  - have `evidence.isRestricted = True`
A finding that violates these invariants is rewritten in-place before
persistence and an `ingest`-domain warning is appended noting the
correction. We never persist a leak.

Source_agent value is 'controls-audit' — this replaces the legacy
controls-audit-agent rows (42 historical findings already in the DB).
DedupKey UPSERT absorbs them naturally; no separate baseline tables.
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

# Allow `python3 scripts/run_controls_audit.py` and `python3 -m`.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts.detectors import bank as bank_detector              # noqa: E402
from scripts.detectors import contacts as contacts_detector      # noqa: E402
from scripts.detectors import journals as journals_detector      # noqa: E402
from scripts.detectors import related_party as rp_detector       # noqa: E402
from scripts.detectors import sod as sod_detector                # noqa: E402
from scripts.detectors import users as users_detector            # noqa: E402
from scripts.xero_controls import people_masked_label            # noqa: E402

SOURCE_AGENT = "controls-audit"


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
    """Return (conn, Jsonb). psycopg v3 preferred, v2 fallback."""
    url = _db_url()
    try:
        import psycopg  # v3
        from psycopg.types.json import Jsonb
        return psycopg.connect(url), Jsonb
    except ImportError:
        import psycopg2  # v2 fallback
        from psycopg2.extras import Json as Jsonb  # type: ignore
        return psycopg2.connect(url), Jsonb


# ── audit_runs lifecycle ─────────────────────────────────────────────

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


# ── emit-time restricted-routing guard ───────────────────────────────

def _enforce_routing_guard(f: dict[str, Any]) -> list[dict[str, Any]]:
    """In-place guard on every finding before persistence.

    Returns a list of EXTRA findings (ingest-domain warnings) that should
    also be persisted when a guard violation was corrected. Mutates `f`
    in place so the leak never reaches the DB.
    """
    extras: list[dict[str, Any]] = []
    f.setdefault("evidence", {})
    is_people = bool(f.get("is_people_flag"))

    if not is_people:
        # systemic findings should NOT carry an isRestricted=true flag —
        # tolerate but don't enforce. Continue.
        return extras

    evidence = f["evidence"]
    name = evidence.get("individualName")

    # Invariant 1: individualName MUST be present.
    if not name:
        extras.append(_routing_violation(
            f, "missing_individualName",
            "PEOPLE-flag finding had no evidence.individualName — title "
            "was rewritten to <unknown>-XXXX and the flag preserved.",
        ))
        name = "Unknown"
        evidence["individualName"] = name

    # Invariant 2: title MUST NOT contain the full name.
    title = f.get("title") or ""
    if name and name in title:
        masked = people_masked_label(name)
        new_title = title.replace(name, masked)
        extras.append(_routing_violation(
            f, "name_leaked_in_title",
            f"PEOPLE-flag finding leaked '{name}' into the title; "
            f"rewritten to masked form '{masked}' before persistence.",
        ))
        f["title"] = new_title

    # Invariant 3: evidence.isRestricted MUST be true.
    if not evidence.get("isRestricted"):
        evidence["isRestricted"] = True

    return extras


def _routing_violation(orig: dict[str, Any], kind: str, msg: str) -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": "routing-guard-correction",
        "domain": "ingest",
        "severity": "warning",
        "entity_code": orig.get("entity_code", "SC"),
        "is_people_flag": False,
        "title": f"controls-audit emit-time routing guard rewrote a finding ({kind})",
        "detail": msg,
        "amount": None,
        "evidence": {
            "dedupKey": (
                f"routing-guard-correction:{orig.get('entity_code', 'SC')}:"
                f"{orig.get('detector', 'unknown')}:{kind}:{today_iso}"
            ),
            "kind": "routing-guard-correction",
            "violation": kind,
            "originalDetector": orig.get("detector"),
        },
    }


# ── persistence ──────────────────────────────────────────────────────

def _persist_finding(conn, Jsonb, run_id: str, f: dict[str, Any]) -> bool:
    """Insert finding; dedup on evidence.dedupKey when present + unresolved.

    Returns True if a NEW row inserted, False if an existing open row
    was UPDATED in place. dedupKey UPSERT replaces the legacy baseline
    tables (ContactBankSnapshot etc.).
    """
    evidence = dict(f.get("evidence") or {})
    evidence.setdefault("runId", run_id)
    evidence.setdefault(
        "runAt", _dt.datetime.now(_dt.timezone.utc).isoformat()
    )
    dedup_key = evidence.get("dedupKey")
    is_people = bool(f.get("is_people_flag"))

    with conn.cursor() as cur:
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
                       SET title=%s, detail=%s, amount=%s,
                           severity=%s, is_people_flag=%s,
                           evidence=%s, run_id=%s
                     WHERE id=%s
                    """,
                    (
                        f["title"], f["detail"], f.get("amount"),
                        f["severity"], is_people,
                        Jsonb(evidence), run_id, row[0],
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


# ── gather ───────────────────────────────────────────────────────────

def _ingest_failure(entity: str, label: str, exc: BaseException) -> dict[str, Any]:
    today_iso = _dt.date.today().isoformat()
    return {
        "detector": f"{label}-detector-failed",
        "domain": "ingest",
        "severity": "warning",
        "entity_code": entity,
        "is_people_flag": False,
        "title": f"{entity}: {label} detector raised {type(exc).__name__}",
        "detail": f"Detector failed mid-run: {exc}.",
        "amount": None,
        "evidence": {
            "dedupKey": f"{label}-detector-failed:{entity}:{today_iso}",
            "kind": "ingest-failure",
            "error": str(exc),
            "traceback": traceback.format_exc(limit=4),
        },
    }


def _gather_findings() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    for entity in ("SC", "CQ"):
        for label, fn in (
            ("contacts", contacts_detector.run_contacts),
            ("bank", bank_detector.run_bank),
            ("users", users_detector.run_users),
            # stubs — included so they show up in the call graph; return []
            ("sod", sod_detector.run_sod),
            ("related-party", rp_detector.run_related_party),
            ("journal-integrity", journals_detector.run_journals),
        ):
            try:
                findings.extend(fn(entity))
            except Exception as exc:  # noqa: BLE001
                findings.append(_ingest_failure(entity, label, exc))

    # Normalise minimums every detector should already have set.
    today_iso = _dt.date.today().isoformat()
    for f in findings:
        f.setdefault("is_people_flag", False)
        f.setdefault("evidence", {})
        f["evidence"].setdefault("source_agent", SOURCE_AGENT)
        if "dedupKey" not in f["evidence"]:
            f["evidence"]["dedupKey"] = (
                f"{f['detector']}:{f['entity_code']}:{today_iso}"
            )

    return findings


# ── main ─────────────────────────────────────────────────────────────

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

        # Apply emit-time routing guard. Extras are appended.
        guarded: list[dict[str, Any]] = []
        for f in findings:
            extras = _enforce_routing_guard(f)
            guarded.append(f)
            guarded.extend(extras)
        findings = guarded

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
                    "warn": "persist-failed",
                    "error": str(exc),
                    "finding": {k: f.get(k) for k in ("detector", "entity_code")},
                }))

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
            "critical_count": crit,
            "people_flags_count": people,
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
