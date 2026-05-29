"""Read-only Xero OAuth2 client_credentials helper for jbc-payables-detector.

Mirrors the shape of jbc-reconciliation/xero_client.py but exposes the
ACCPAY / Contacts endpoints this skill needs. No write methods.
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
    "accounting.contacts.read "
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


# ── endpoint helpers ──────────────────────────────────────────────────

def list_accpay_invoices(entity: str, *, since_iso: str) -> list[dict[str, Any]]:
    """All ACCPAY (supplier bills) modified since `since_iso`. Paginated."""
    results: list[dict[str, Any]] = []
    page = 1
    where = 'Type=="ACCPAY"'
    while True:
        params = {
            "page": page,
            # Xero uses an If-Modified-Since-style header normally; the
            # `where` clause filters by Date instead so we get the full
            # bill body in one shot.
        }
        try:
            data = _get(entity, "Invoices", params=params, where=where)
        except urllib.error.HTTPError:
            break
        chunk = list(data.get("Invoices") or [])
        if not chunk:
            break
        # Client-side date filter so we don't smash old data forever.
        since_dt = _dt.datetime.fromisoformat(since_iso)
        kept = []
        for inv in chunk:
            d = parse_xero_date(inv.get("Date")) or parse_xero_date(inv.get("UpdatedDateUTC"))
            if d is None or d.replace(tzinfo=None) >= since_dt:
                kept.append(inv)
        results.extend(kept)
        if len(chunk) < 100:
            break
        page += 1
        if page > 50:
            break
    return results


def list_supplier_contacts(entity: str) -> list[dict[str, Any]]:
    """All Contacts flagged IsSupplier==true. Paginated."""
    results: list[dict[str, Any]] = []
    page = 1
    where = "IsSupplier==true"
    while True:
        try:
            data = _get(entity, "Contacts", params={"page": page}, where=where)
        except urllib.error.HTTPError:
            break
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


# ── small parsing utilities ──────────────────────────────────────────

def parse_xero_date(value: Any) -> _dt.datetime | None:
    """Xero serialises dates as either ISO strings or `/Date(1234567890000+0000)/`."""
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
