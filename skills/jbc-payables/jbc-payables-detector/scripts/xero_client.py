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
