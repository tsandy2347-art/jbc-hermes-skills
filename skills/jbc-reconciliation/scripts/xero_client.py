"""Read-only Xero OAuth2 client_credentials helper for jbc-reconciliation.

Two tenants — SC and CQ — selected by the `entity` argument ("SC" | "CQ").
Env vars per tenant: XERO_<entity>_CLIENT_ID / _CLIENT_SECRET / _TENANT_ID.

The endpoints we touch are READ-ONLY. The granted scope list is the minimum
required for the three detector domains. No write scopes.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOKEN_URL = "https://identity.xero.com/connect/token"
API_BASE = "https://api.xero.com/api.xro/2.0"
READ_SCOPES = (
    "accounting.transactions.read "
    "accounting.journals.read "
    "accounting.reports.read "
    "accounting.settings.read"
)

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _creds(entity: str) -> dict[str, str]:
    e = entity.upper()
    if e not in ("SC", "CQ"):
        raise ValueError(f"entity must be SC or CQ, got {entity!r}")
    cid = os.environ.get(f"XERO_{e}_CLIENT_ID", "")
    cs = os.environ.get(f"XERO_{e}_CLIENT_SECRET", "")
    tid = os.environ.get(f"XERO_{e}_TENANT_ID", "")
    if not cid or not cs or not tid:
        raise RuntimeError(
            f"XERO_{e}_CLIENT_ID / _CLIENT_SECRET / _TENANT_ID must be set"
        )
    return {"client_id": cid, "client_secret": cs, "tenant_id": tid}


def _token(entity: str) -> str:
    e = entity.upper()
    cached = _TOKEN_CACHE.get(e)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    creds = _creds(e)
    basic = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()
    ).decode()
    body = f"grant_type=client_credentials&scope={urllib.parse.quote(READ_SCOPES)}".encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e_:
        raise RuntimeError(
            f"Xero token exchange failed for {entity}: {e_.code} {e_.read().decode()[:300]}"
        ) from e_

    tok = data["access_token"]
    exp = time.time() + int(data.get("expires_in", 1800))
    _TOKEN_CACHE[e] = (tok, exp)
    return tok


def _get(entity: str, path: str, params: dict[str, Any] | None = None,
         where: str | None = None) -> dict[str, Any]:
    creds = _creds(entity)
    tok = _token(entity)
    qp = dict(params or {})
    if where:
        qp["where"] = where
    qs = ("?" + urllib.parse.urlencode(qp, quote_via=urllib.parse.quote)) if qp else ""
    req = urllib.request.Request(
        f"{API_BASE}/{path}{qs}",
        headers={
            "Authorization": f"Bearer {tok}",
            "Xero-Tenant-Id": creds["tenant_id"],
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
