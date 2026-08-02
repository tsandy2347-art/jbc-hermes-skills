"""Domain C — journal posting integrity.

Emits:
  unposted-manual-journal   (warning, escalates to critical > 5 business days)
  late-posted-journal       (warning, aggregate per entity)
  large-posted-journal      (warning, per-journal)
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from ..xero_client import list_manual_journals, list_recent_journals, parse_xero_date


def _fmt_aud(n: float) -> str:
    return f"A${n:,.2f}"


def _business_days_between(a: _dt.datetime, b: _dt.datetime) -> int:
    if a > b:
        a, b = b, a
    days = (b.date() - a.date()).days
    if days <= 0:
        return 0
    full_weeks, rem = divmod(days, 7)
    count = full_weeks * 5
    start_dow = a.weekday()
    for i in range(rem):
        if (start_dow + i) % 7 < 5:
            count += 1
    return count


def _mj_amount(j: dict[str, Any]) -> float:
    return sum(
        float(l.get("LineAmount") or 0)
        for l in (j.get("JournalLines") or [])
        if float(l.get("LineAmount") or 0) > 0
    )


def _xero_link(mj_id: str) -> str:
    return f"https://go.xero.com/Bank/ViewManualJournal.aspx?ManualJournalID={mj_id}"


def run_journal(
    entity: str,
    *,
    lag_days: int,
    large_threshold_aud: float,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    today = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=10))
    )
    today_iso = today.date().isoformat()

    try:
        manuals = list_manual_journals(entity)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "detector": "unposted-manual-journal",
            "domain": "ingest",
            "severity": "warning",
            "entity_code": entity,
            "title": f"{entity}: ManualJournals endpoint failed",
            "detail": f"Could not list manual journals from Xero: {exc}",
            "amount": None,
            "evidence": {"dedupKey": f"mj-list-failed:{entity}",
                         "error": str(exc)},
        })
        manuals = []

    # 1. DRAFT manual journals — one finding per journal.
    for j in manuals:
        if j.get("Status") != "DRAFT":
            continue
        mj_id = j.get("ManualJournalID", "")
        d = parse_xero_date(j.get("Date"))
        age = _business_days_between(d, today) if d else 0
        amount = _mj_amount(j)
        narration = (j.get("Narration") or "").strip() or "(no narration)"
        trunc = narration if len(narration) <= 80 else narration[:77] + "…"
        line_count = len(j.get("JournalLines") or [])
        link = _xero_link(mj_id)

        findings.append({
            "detector": "unposted-manual-journal",
            "domain": "journal",
            "severity": "critical" if age > 5 else "warning",
            "entity_code": entity,
            "title": f"Unposted manual journal — {trunc} ({_fmt_aud(amount)}, {age}d old)",
            "detail": (
                "Draft journals sit outside the GL and skew month-to-date figures. "
                f"Narration: \"{narration}\". Amount: {_fmt_aud(amount)}. {line_count} line(s). "
                f"Oldest line date {age} business day(s) ago. Open in Xero to post or void: {link}"
            ),
            "amount": amount,
            "evidence": {
                "dedupKey": f"unposted-mj:{entity}:{mj_id}",
                "kind": "unposted-manual-journal",
                "ManualJournalID": mj_id, "narration": narration,
                "amount": amount, "lineCount": line_count,
                "journalDate": j.get("Date"), "ageBusinessDays": age,
                "xeroLink": link,
            },
        })

    # 2. Late-posted journals — aggregate per entity, top 10 in evidence.
    try:
        recent = list_recent_journals(entity, offset=0)
    except Exception:
        recent = []

    lagged: list[dict[str, Any]] = []
    for j in recent:
        jd = parse_xero_date(j.get("JournalDate"))
        created = parse_xero_date(j.get("CreatedDateUTC"))
        if not jd or not created:
            continue
        lag = _business_days_between(jd, created)
        if lag > lag_days:
            lagged.append({
                "JournalID": j.get("JournalID"),
                "lagBusinessDays": lag,
                "journalDate": j.get("JournalDate"),
            })
    if lagged:
        lagged.sort(key=lambda r: r["lagBusinessDays"], reverse=True)
        worst = lagged[0]
        findings.append({
            "detector": "late-posted-journal",
            "domain": "journal",
            "severity": "warning",
            "entity_code": entity,
            "title": f"{len(lagged)} journal(s) posted late",
            "detail": (
                f"Posted more than {lag_days} business days after their transaction date. "
                f"Worst lag: {worst['lagBusinessDays']} days "
                f"(journal {str(worst['JournalID'])[:8]}…). "
                "Investigate the close cadence."
            ),
            "amount": None,
            "evidence": {
                "dedupKey": f"late-posted-aggregate:{entity}",
                "kind": "late-posted-journals",
                "totalCount": len(lagged),
                "lagThresholdBusinessDays": lag_days,
                "worstLag": worst["lagBusinessDays"],
                "top10": lagged[:10],
            },
        })

    # 3. Large posted manual journals — one finding per MJ.
    for j in manuals:
        if j.get("Status") != "POSTED":
            continue
        amount = _mj_amount(j)
        if amount < large_threshold_aud:
            continue
        mj_id = j.get("ManualJournalID", "")
        narration = (j.get("Narration") or "").strip() or "(no narration)"
        trunc = narration if len(narration) <= 80 else narration[:77] + "…"
        link = _xero_link(mj_id)
        findings.append({
            "detector": "large-posted-journal",
            "domain": "journal",
            "severity": "warning",
            "entity_code": entity,
            "title": f"Large manual journal — {trunc} ({_fmt_aud(amount)})",
            "detail": (
                f"Posted manual journal of {_fmt_aud(amount)} "
                f"(≥ {_fmt_aud(large_threshold_aud)} threshold). "
                f"Narration: \"{narration}\". Confirm supporting evidence. Open in Xero: {link}"
            ),
            "amount": amount,
            "evidence": {
                "dedupKey": f"large-mj:{entity}:{mj_id}",
                "kind": "large-posted-manual-journal",
                "ManualJournalID": mj_id, "narration": narration,
                "amount": amount, "journalDate": j.get("Date"),
                "xeroLink": link,
            },
        })

    return findings
