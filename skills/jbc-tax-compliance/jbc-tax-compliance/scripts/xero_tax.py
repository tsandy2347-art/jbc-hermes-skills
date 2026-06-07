"""Read-only Xero helper for jbc-tax-compliance.

Now backed by the shared `xero_pulse` token store (refresh-token flow against
the Pulse Xero app). The old per-entity client_credentials env vars
(XERO_SC_CLIENT_ID etc.) are no longer consulted — credentials live in
mark-agent's XeroTenantToken table.

READ scopes only. No write paths.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Shared token helper lives at /data/hermes/lib/xero_pulse.py
sys.path.insert(0, "/data/hermes/lib")
from xero_pulse import get_pulse_token, tenant_configured as _pulse_configured  # noqa: E402

API_BASE = "https://api.xero.com/api.xro/2.0"


def tenant_configured(entity: str) -> bool:
    """Drop-in replacement — was env-var-presence check, now is DB-row check."""
    return _pulse_configured(entity)


def _get(entity: str, path: str, params: dict[str, Any] | None = None,
         where: str | None = None) -> dict[str, Any]:
    tok, tenant_id = get_pulse_token(entity)
    qp = dict(params or {})
    if where:
        qp["where"] = where
    qs = ("?" + urllib.parse.urlencode(qp, quote_via=urllib.parse.quote)) if qp else ""
    req = urllib.request.Request(
        f"{API_BASE}/{path}{qs}",
        headers={
            "Authorization": f"Bearer {tok}",
            "Xero-Tenant-Id": tenant_id,
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# ── endpoint helpers ─────────────────────────────────────────────────

def list_accounts(entity: str) -> list[dict[str, Any]]:
    data = _get(entity, "Accounts")
    return list(data.get("Accounts") or [])


def list_journals_since(entity: str, since: _dt.datetime,
                        max_pages: int = 50) -> list[dict[str, Any]]:
    """Paginate /Journals by offset until JournalDate < since, or pages exhausted.

    Xero /Journals returns 100 most-recent journals after `offset` (an
    integer JournalNumber). We walk newest-first; we keep pulling until
    the last batch's earliest journal pre-dates `since`.
    """
    results: list[dict[str, Any]] = []
    offset = 0
    seen_journal_numbers: set[int] = set()
    for _ in range(max_pages):
        data = _get(entity, "Journals", params={"offset": offset})
        batch = list(data.get("Journals") or [])
        if not batch:
            break
        results.extend(batch)
        # next offset = max JournalNumber in batch
        nums: list[int] = []
        oldest_date: _dt.datetime | None = None
        for j in batch:
            try:
                num = int(j.get("JournalNumber") or 0)
                nums.append(num)
                seen_journal_numbers.add(num)
            except (TypeError, ValueError):
                pass
            jd = parse_xero_date(j.get("JournalDate"))
            if jd is not None:
                if oldest_date is None or jd < oldest_date:
                    oldest_date = jd
        if not nums:
            break
        next_offset = max(nums)
        if next_offset <= offset:
            break
        offset = next_offset
        if oldest_date is not None and oldest_date < since:
            break
        if len(batch) < 100:
            break
    return results


def trial_balance(entity: str, date_iso: str) -> dict[str, Any] | None:
    try:
        return _get(entity, "Reports/TrialBalance", params={"date": date_iso})
    except urllib.error.HTTPError:
        return None


def profit_and_loss(entity: str, from_iso: str, to_iso: str) -> dict[str, Any] | None:
    try:
        return _get(
            entity, "Reports/ProfitAndLoss",
            params={"fromDate": from_iso, "toDate": to_iso},
        )
    except urllib.error.HTTPError:
        return None


# ── report parsing ───────────────────────────────────────────────────

def find_account_balance(report: dict[str, Any] | None,
                         account_code: str,
                         account_name: str | None = None) -> float | None:
    """Walk a Xero Reports response and return the numeric balance for an
    account identified by Code (preferred) or Name (fallback).

    Xero report row shape: rows[].Cells[].Value plus optional Attributes
    `[{Id: 'account', Value: '<GUID>'}, {Id: 'code', Value: 'XXX'}]`.
    """
    if not report:
        return None
    reports = report.get("Reports") or []
    for r in reports:
        for row in _walk_rows(r.get("Rows") or []):
            cells = row.get("Cells") or []
            if not cells:
                continue
            # 1) match by Attributes.code
            for cell in cells:
                for attr in (cell.get("Attributes") or []):
                    aid = (attr.get("Id") or "").lower()
                    aval = str(attr.get("Value") or "")
                    if aid == "code" and aval == account_code:
                        return _cell_amount(cells)
            # 2) match by cell text containing code prefix or full name
            first_val = str((cells[0] or {}).get("Value") or "")
            if account_code and (
                first_val == account_code
                or first_val.startswith(f"{account_code} ")
                or first_val.startswith(f"{account_code}-")
            ):
                return _cell_amount(cells)
            if account_name and first_val.lower() == account_name.lower():
                return _cell_amount(cells)
    return None


def _walk_rows(rows: list[dict[str, Any]]):
    for row in rows:
        rtype = row.get("RowType")
        if rtype == "Section":
            for inner in _walk_rows(row.get("Rows") or []):
                yield inner
        elif rtype == "Row" or rtype == "SummaryRow":
            yield row


def _cell_amount(cells: list[dict[str, Any]]) -> float | None:
    # Last cell is usually the amount; try right-to-left.
    for cell in reversed(cells):
        v = cell.get("Value")
        if v in (None, ""):
            continue
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            continue
    return None


# ── parsing utilities ────────────────────────────────────────────────

def parse_xero_date(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    s = str(value)
    if s.startswith("/Date(") and s.endswith(")/"):
        inner = s[6:-2]
        for sep in ("+", "-"):
            if sep in inner[1:]:
                inner = inner.split(sep, 1)[0]
                break
        try:
            return _dt.datetime.fromtimestamp(int(inner) / 1000, tz=_dt.timezone.utc)
        except (ValueError, OverflowError):
            return None
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


# ── derived helpers used by detectors ────────────────────────────────

def aggregate_tax_types(journals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return {taxType: {taxType, taxName, net, tax, lineCount}}."""
    out: dict[str, dict[str, Any]] = {}
    for j in journals:
        for line in (j.get("JournalLines") or []):
            key = line.get("TaxType") or "UNTAGGED"
            agg = out.get(key) or {
                "taxType": key, "taxName": line.get("TaxName"),
                "net": 0.0, "tax": 0.0, "lineCount": 0,
            }
            try:
                agg["net"] += float(line.get("NetAmount") or 0)
            except (TypeError, ValueError):
                pass
            try:
                agg["tax"] += float(line.get("TaxAmount") or 0)
            except (TypeError, ValueError):
                pass
            agg["lineCount"] += 1
            out[key] = agg
    return out


def split_codes(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]