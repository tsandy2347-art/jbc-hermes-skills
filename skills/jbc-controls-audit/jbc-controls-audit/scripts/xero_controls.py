"""Read-only Xero OAuth2 client_credentials helper for jbc-controls-audit.

Same shape as jbc-reconciliation/xero_client.py — two tenants (SC, CQ)
selected by `entity`. Endpoints here: Users, Contacts, BankAccounts
(via Accounts where Type=BANK), ManualJournals. Scope list is the
minimum required and READ-ONLY.
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
    "accounting.contacts.read "
    "accounting.settings.read "
    "accounting.transactions.read "
    "accounting.journals.read"
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


# ── endpoint helpers ─────────────────────────────────────────────────

def list_users(entity: str) -> list[dict[str, Any]]:
    """Xero org users — for elevated-user-roster + (future) SoD."""
    data = _get(entity, "Users")
    return list(data.get("Users") or [])


def list_contacts(entity: str, *, suppliers_only: bool = True) -> list[dict[str, Any]]:
    """Contacts list — paginated. Suppliers only by default."""
    results: list[dict[str, Any]] = []
    page = 1
    where = 'IsSupplier==true' if suppliers_only else None
    while True:
        params: dict[str, Any] = {"page": page}
        data = _get(entity, "Contacts", params=params, where=where)
        chunk = list(data.get("Contacts") or [])
        if not chunk:
            break
        results.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 50:
            break
    return results


def list_bank_accounts(entity: str) -> list[dict[str, Any]]:
    data = _get(entity, "Accounts", where='Type=="BANK"')
    return list(data.get("Accounts") or [])


def list_manual_journals(entity: str) -> list[dict[str, Any]]:
    data = _get(entity, "ManualJournals")
    return list(data.get("ManualJournals") or [])


# ── parsing / masking utilities ──────────────────────────────────────

def parse_xero_date(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value
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
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def mask_account(number: str | None) -> str:
    if not number:
        return "***"
    digits = "".join(ch for ch in str(number) if ch.isdigit())
    last3 = digits[-3:] or "***"
    return f"***{last3}"


def initials(name: str | None) -> str:
    """Return uppercase initials of `name`, e.g. 'John Smith' -> 'JS'.

    Used in PEOPLE-flagged finding titles. Falls back to 'X' when empty.
    """
    if not name:
        return "X"
    parts = [p for p in str(name).replace(",", " ").split() if p]
    if not parts:
        return "X"
    letters = "".join(p[0].upper() for p in parts[:3] if p[0].isalpha())
    return letters or "X"


def people_masked_label(name: str | None) -> str:
    """'<initials>-XXXX' form used in title for PEOPLE-flagged findings."""
    return f"{initials(name)}-XXXX"
