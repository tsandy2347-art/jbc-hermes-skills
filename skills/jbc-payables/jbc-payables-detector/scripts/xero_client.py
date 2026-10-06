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
from xero_pulse import (  # noqa: E402
    force_refresh,
    get_pulse_token,
    tenant_configured as _pulse_configured,
)

API_BASE = "https://api.xero.com/api.xro/2.0"

# Rate-limit backoff: Xero allows 60 calls/min and 5,000/day per app per tenant.
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_MAX_ATTEMPTS = 3


def tenant_configured(entity: str) -> bool:
    """Drop-in replacement — was env-var-presence check, now is DB-row check."""
    return _pulse_configured(entity)


def _request(entity: str, path: str, qs: str, tok: str, tenant_id: str) -> dict[str, Any]:
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


def _get(entity: str, path: str, params: dict[str, Any] | None = None,
         where: str | None = None) -> dict[str, Any]:
    """One Xero GET.

    Raises on failure — deliberately. This used to be wrapped by callers in a
    bare `except HTTPError: break`, which turned an auth failure into an empty
    result set, which the detector then read as "no problems found". A 401
    means we learned nothing; it must never look like a clean scan.
    """
    qp = dict(params or {})
    if where:
        qp["where"] = where
    qs = ("?" + urllib.parse.urlencode(qp, quote_via=urllib.parse.quote)) if qp else ""

    tok, tenant_id = get_pulse_token(entity)
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return _request(entity, path, qs, tok, tenant_id)
        except urllib.error.HTTPError as exc:
            # 401: the stored expiry claimed this token was live and Xero says
            # otherwise. Xero is the authority — mint a new one and retry once.
            if exc.code == 401 and attempt == 0:
                tok, tenant_id = force_refresh(entity)
                continue
            if exc.code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
                retry_after = int(exc.headers.get("Retry-After", "5") or 5)
                # A daily-quota 429 can ask for hours; don't hold the run hostage.
                if exc.headers.get("X-Rate-Limit-Problem") == "day":
                    raise
                time.sleep(min(60, retry_after + 1))
                continue
            raise
    raise RuntimeError(f"Xero GET {path} for {entity} exhausted {_MAX_ATTEMPTS} attempts")


# ── endpoint helpers ──────────────────────────────────────────────────

# All four detector groups want the same two snapshots, so without this the
# run pulls the full paginated bill list four times per entity — eight
# identical scans of up to 50 pages each. That is what made a working run take
# long enough to be killed mid-flight and leave its audit_runs row stuck at
# 'running'. Snapshots are immutable for the life of one run, so cache them.
_SNAPSHOT_CACHE: dict[tuple[str, str, str], list[dict[str, Any]]] = {}


def clear_snapshot_cache() -> None:
    _SNAPSHOT_CACHE.clear()


class BillFetchTruncated(RuntimeError):
    """A single day of bills exceeded Xero's 5,000-row page cap. Raised, never
    returned short: payables detectors are swept, so a partial list would
    close findings for bills that were simply not fetched."""


def _accpay_between(entity: str, lo: _dt.date, hi: _dt.date | None) -> tuple[list[dict[str, Any]], bool]:
    where = f'Type=="ACCPAY" AND Date >= DateTime({lo.year},{lo.month:02d},{lo.day:02d})'
    if hi is not None:
        where += f' AND Date <= DateTime({hi.year},{hi.month:02d},{hi.day:02d})'
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        # No try/except here on purpose. A failed page means we do not know
        # what is in Xero; the caller records that as an ingest-failure finding
        # rather than reporting a short, clean-looking result set.
        data = _get(entity, "Invoices", params={"page": page}, where=where)
        chunk = list(data.get("Invoices") or [])
        if not chunk:
            return results, False
        results.extend(chunk)
        if len(chunk) < 100:
            return results, False
        page += 1
        if page > 50:
            return results, True


def list_accpay_invoices(entity: str, *, since_iso: str, slice_days: int = 10) -> list[dict[str, Any]]:
    """All ACCPAY (supplier bills) dated since `since_iso`, complete.

    Fetched in date slices so no request reaches Xero's 5,000-row cap (a
    slice that does is halved until it fits). Until Oct 2026 this was one
    capped request: SC has ~6,700 bills in 90 days, so ~1,700 were silently
    missing from every payables run. Raises BillFetchTruncated if a single
    day exceeds the cap.
    """
    cache_key = ("accpay", entity, since_iso)
    cached = _SNAPSHOT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    lo = _dt.date.fromisoformat(since_iso[:10])
    today = _dt.date.today()
    out: dict[str, dict[str, Any]] = {}

    def fetch(a: _dt.date, b: _dt.date | None) -> None:
        rows, capped = _accpay_between(entity, a, b)
        if capped:
            if b is None or a >= b:
                raise BillFetchTruncated(
                    f"{entity}: more than 5,000 bills dated {a.isoformat()}"
                    + ("" if b else " onwards")
                )
            mid = a + (b - a) // 2
            fetch(a, mid)
            fetch(mid + _dt.timedelta(days=1), b)
            return
        for r in rows:
            out[r.get("InvoiceID") or id(r)] = r

    cur = lo
    while cur <= today:
        stop = min(cur + _dt.timedelta(days=slice_days - 1), today)
        fetch(cur, stop)
        cur = stop + _dt.timedelta(days=1)
    fetch(today + _dt.timedelta(days=1), None)  # future-dated bills
    results = list(out.values())
    _SNAPSHOT_CACHE[cache_key] = results
    return results


def list_supplier_contacts(entity: str) -> list[dict[str, Any]]:
    """All Contacts flagged IsSupplier==true. Paginated."""
    cache_key = ("suppliers", entity, "")
    cached = _SNAPSHOT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    results: list[dict[str, Any]] = []
    page = 1
    where = "IsSupplier==true"
    while True:
        # See list_accpay_invoices — errors propagate so a broken scan is
        # reported as broken, not as "no suppliers found".
        data = _get(entity, "Contacts", params={"page": page}, where=where)
        chunk = list(data.get("Contacts") or [])
        if not chunk:
            break
        results.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 50:
            break
    _SNAPSHOT_CACHE[cache_key] = results
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
