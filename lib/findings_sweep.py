"""Resolve-on-absence for the shared JBC findings DB.

Why this exists
---------------
Every detector could OPEN a finding; nothing could CLOSE one. A condition
stayed open until a human clicked it, so the findings table only ever grew:
by 2 Aug 2026 it held 5,456 open rows of which 4,792 (88%) were more than a
month old, including 3,902 payroll rows from a detector that had not written
anything since 26 June. Mark's daily brief was assembled from that pile, which
is why it read as noise — almost none of it was still true.

The rule
--------
Only STATE detectors are swept, and each runner must name its own.

This distinction is the whole design, and getting it wrong loses real findings:

  * A **state** detector answers "how are things right now" — this account is
    overdrawn, this supplier has no ABN, this journal is unposted, this invoice
    is 90 days old. Its output is a complete snapshot every run, so anything
    missing from the new snapshot has genuinely cleared. Safe to sweep.

  * An **event** detector answers "this happened" — a payment went to an
    unvetted vendor, a supplier's bank details changed, a duplicate bill was
    raised. It only looks back over a window, so an event dropping out of the
    window means the window moved, NOT that the issue was dealt with. Sweeping
    these silently closes open problems.

Tried the naive way first on 2 Aug 2026: sweeping every detector resolved 305
of controls-audit's 308 open findings in one run, including unvetted-vendor
payments and bank-detail changes that were still entirely unresolved. Hence
the allowlist — a detector is swept only if its runner explicitly says it is
safe to sweep.

The safety rules matter as much as the rule:

1. **A run with any ingest failure sweeps nothing.** If a detector group threw,
   we do not know what we failed to see, and "I didn't look" must never be
   recorded as "it's fixed". This is the same principle as the brief's coverage
   section: absence of evidence is not evidence of absence.
2. **A sweep larger than FINDINGS_SWEEP_MAX aborts.** A detector bug that
   emits nothing would otherwise silently close the entire board. Above the
   limit we resolve nothing and say so loudly, so a human looks.
3. **Resolution is attributed and reversible.** Rows are marked
   resolved_by='auto:absent-from-run' with the run id in the note, so a bad
   sweep can be identified and undone with one UPDATE.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

# A single run resolving more than this many findings is treated as a bug, not
# as good news. Tunable per-agent via env if a legitimately huge cleanup is
# expected.
DEFAULT_SWEEP_MAX = 1000

RESOLVED_BY = "auto:absent-from-run"


def sweep_max() -> int:
    raw = os.environ.get("FINDINGS_SWEEP_MAX", "")
    try:
        return int(raw) if raw else DEFAULT_SWEEP_MAX
    except ValueError:
        return DEFAULT_SWEEP_MAX


def identity_of(source_agent: str, finding: dict[str, Any]) -> str:
    """The key a finding is matched on across runs.

    Mirrors the two-tier dedup in every runner's `_persist_finding`: an explicit
    evidence.dedupKey when the detector set one, otherwise the
    (agent, entity, title) tuple the fallback path dedups on. The sweep MUST use
    the same identity the writer used, or it would resolve rows that were in
    fact just re-emitted.
    """
    ev = finding.get("evidence") or {}
    key = ev.get("dedupKey")
    if key:
        return str(key)
    return f"{source_agent}:{finding.get('entity_code', '')}:{finding.get('title', '')}"


def resolve_absent(
    conn,
    *,
    source_agent: str,
    run_id: str,
    emitted: Iterable[str],
    had_failures: bool,
    state_detectors: Iterable[str],
) -> dict[str, Any]:
    """Close open STATE findings this run did not re-emit.

    `state_detectors` is the runner's explicit list of detectors whose output is
    a complete current-state snapshot. Anything not on it is left alone — see
    the module docstring for why that asymmetry is deliberate.
    """
    emitted_keys = sorted(set(emitted))
    sweepable = sorted(set(state_detectors))

    if had_failures:
        return {
            "swept": False,
            "reason": "run had ingest failures — cannot distinguish 'gone' from 'not looked at'",
            "resolved": 0,
        }

    if not sweepable:
        return {
            "swept": False,
            "reason": "no state detectors declared for this agent — nothing is safe to auto-close",
            "resolved": 0,
        }

    limit = sweep_max()

    # Match on the same identity the writer dedups on.
    identity_sql = (
        "COALESCE(evidence->>'dedupKey', "
        "source_agent || ':' || entity_code || ':' || title)"
    )
    where = [
        "source_agent = %(agent)s",
        "resolved = false",
        "detector = ANY(%(sweepable)s)",
        f"NOT ({identity_sql} = ANY(%(emitted)s))",
    ]
    where_sql = " AND ".join(where)
    params = {"agent": source_agent, "emitted": emitted_keys, "sweepable": sweepable}

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM findings WHERE {where_sql}", params)
        candidate_count = int(cur.fetchone()[0])

        if candidate_count > limit:
            return {
                "swept": False,
                "reason": (
                    f"would resolve {candidate_count} findings, over the {limit} safety limit — "
                    f"refusing. This usually means the detector emitted nothing, not that "
                    f"{candidate_count} problems were fixed."
                ),
                "resolved": 0,
                "would_have_resolved": candidate_count,
            }

        if candidate_count == 0:
            return {"swept": True, "resolved": 0, "emitted": len(emitted_keys)}

        cur.execute(
            f"""
            UPDATE findings
               SET resolved = true,
                   resolved_by = %(by)s,
                   resolved_at = now(),
                   resolution_note = %(note)s
             WHERE {where_sql}
            """,
            {**params, "by": RESOLVED_BY, "note": f"not re-emitted by clean run {run_id}"},
        )
        resolved = cur.rowcount
    conn.commit()

    return {"swept": True, "resolved": resolved, "emitted": len(emitted_keys)}
