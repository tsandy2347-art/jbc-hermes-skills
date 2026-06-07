"""Read-only Xero helper — now backed by xero_pulse refresh-token flow.

Credentials live in mark-agent's XeroTenantToken DB table, populated by the
OAuth flow at /api/xero/connect on mark-agent. The old per-entity
client_credentials env vars (XERO_SC_CLIENT_ID etc.) are NO LONGER consulted.

READ scopes only. No write paths.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import time
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


# ── endpoint helpers (the only ones the detectors call) ───────────────

def list_bank_accounts(entity: str) -> list[dict[str, Any]]:
    """Return only Type=BANK accounts from the CoA."""
    data = _get(entity, "Accounts", where='Type=="BANK"')
    return list(data.get("Accounts") or [])


def list_unreconciled_bank_transactions(
    entity: str, account_id: str, *, since_iso: str
) -> list[dict[str, Any]]:
    """All unreconciled bank txns on `account_id` since `since_iso`. Paginated."""
    results: list[dict[str, Any]] = []
    page = 1
    where = f'BankAccount.AccountID==Guid("{account_id}") AND IsReconciled==false'
    while True:
        params = {"page": page, "ModifiedAfter": since_iso}
        data = _get(entity, "BankTransactions", params=params, where=where)
        chunk = list(data.get("BankTransactions") or [])
        if not chunk:
            break
        results.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 50:  # safety
            break
    return results


def list_manual_journals(entity: str) -> list[dict[str, Any]]:
    """All recent manual journals (DRAFT + POSTED + VOIDED)."""
    data = _get(entity, "ManualJournals")
    return list(data.get("ManualJournals") or [])


def list_recent_journals(entity: str, offset: int = 0) -> list[dict[str, Any]]:
    """The Journals endpoint returns the 100 most-recent journals after `offset`."""
    data = _get(entity, "Journals", params={"offset": offset})
    return list(data.get("Journals") or [])


def trial_balance(entity: str, date_iso: str) -> dict[str, Any] | None:
    try:
        return _get(entity, "Reports/TrialBalance", params={"date": date_iso})
    except urllib.error.HTTPError:
        return None


def bank_summary(entity: str, from_iso: str, to_iso: str) -> dict[str, Any] | None:
    try:
        return _get(
            entity, "Reports/BankSummary",
            params={"fromDate": from_iso, "toDate": to_iso},
        )
    except urllib.error.HTTPError:
        return None


# ── small parsing utilities used by detectors ────────────────────────

def parse_xero_date(value: Any) -> _dt.datetime | None:
    """Xero serialises dates as either ISO strings or `/Date(1234567890000+0000)/`."""
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value
    s = str(value)
    if s.startswith("/Date(") and s.endswith(")/"):
        inner = s[6:-2]
        # strip timezone suffix like "+0000"
        for sep in ("+", "-"):
            if sep in inner[1:]:  # don't match leading '-'
                inner = inner.split(sep, 1)[0]
                break
        try:
            return _dt.datetime.fromtimestamp(int(inner) / 1000, tz=_dt.timezone.utc)
        except (ValueError, OverflowError):
            return None
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def mask_account(number: str | None) -> str:
    if not number:
        return "••••"
    s = str(number).replace(" ", "").replace("-", "")
    if len(s) <= 4:
        return f"••••{s}"
    return f"••••{s[-4:]}"
