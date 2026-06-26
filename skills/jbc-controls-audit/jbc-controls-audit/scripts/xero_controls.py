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
         where: str | None = None, order: str | None = None) -> dict[str, Any]:
    tok, tenant_id = get_pulse_token(entity)
    qp = dict(params or {})
    if where:
        qp["where"] = where
    if order:
        qp["order"] = order
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
    # Xero throttles at 60 calls/min/tenant; paging many pages back-to-back
    # intermittently trips 429/503. Retry with backoff (honouring Retry-After)
    # so a transient throttle doesn't fail the whole detector for the day.
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                delay = float(retry_after) if retry_after else 0.0
            except ValueError:
                delay = 0.0
            time.sleep(max(delay, 2.0 * (attempt + 1)))
        except urllib.error.URLError as e:
            last_exc = e
            time.sleep(2.0 * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Xero GET {path} exhausted retries")


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


def list_bills(entity: str, *, from_iso: str | None = None,
               to_iso: str | None = None) -> list[dict[str, Any]]:
    """ACCPAY invoices (supplier bills), paginated.

    from_iso / to_iso constrain the bill Date. Strings of the form YYYY-MM-DD.
    """
    where_parts = ['Type=="ACCPAY"']
    if from_iso:
        y, m, d = from_iso[:4], from_iso[5:7], from_iso[8:10]
        if y.isdigit() and m.isdigit() and d.isdigit():
            where_parts.append(f"Date>=DateTime({int(y)},{int(m)},{int(d)})")
    if to_iso:
        y, m, d = to_iso[:4], to_iso[5:7], to_iso[8:10]
        if y.isdigit() and m.isdigit() and d.isdigit():
            where_parts.append(f"Date<=DateTime({int(y)},{int(m)},{int(d)})")
    where = " AND ".join(where_parts)

    # Newest-first. The endpoint caps hard at 5000 rows (50 pages × 100);
    # without an explicit order Xero returns oldest-first, so on a tenant
    # with >5000 bills in the window the cap silently drops the most RECENT
    # bills — exactly the ones a daily control must see. Date DESC guarantees
    # recent coverage; pair with a bounded window in the caller.
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _get(entity, "Invoices", params={"page": page}, where=where,
                    order="Date DESC")
        chunk = list(data.get("Invoices") or [])
        if not chunk:
            break
        results.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 50:
            break
    return results


def list_payments(entity: str) -> list[dict[str, Any]]:
    """All payments, paginated. Used to spot paid-but-returned tickets."""
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _get(entity, "Payments", params={"page": page})
        chunk = list(data.get("Payments") or [])
        if not chunk:
            break
        results.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 50:
            break
    return results


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
