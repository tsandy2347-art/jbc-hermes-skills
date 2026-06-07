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


def _get(
    entity: str,
    path: str,
    params: dict[str, Any] | None = None,
    where: str | None = None,
    order: str | None = None,
    if_modified_since: str | None = None,
) -> dict[str, Any]:
    tok, tenant_id = get_pulse_token(entity)

    qp = dict(params or {})
    if where:
        qp["where"] = where
    if order:
        qp["order"] = order
    qs = ("?" + urllib.parse.urlencode(qp, quote_via=urllib.parse.quote)) if qp else ""

    headers = {
        "Xero-Tenant-Id": tenant_id,
        "Accept": "application/json",
    }
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since

    for attempt in range(3):
        tok, tenant_id = get_pulse_token(entity)
        req = urllib.request.Request(
            f"{API_BASE}/{path}{qs}",
            headers={**headers, "Authorization": f"Bearer {tok}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                retry_after = int(exc.headers.get("Retry-After", "5"))
                time.sleep(min(60, retry_after + 1))
                continue
            raise
    raise RuntimeError(f"Xero GET {path} exhausted retries")


# ── endpoint helpers (the ones the AR detectors call) ────────────────

def list_outstanding_sales_invoices(entity: str) -> list[dict[str, Any]]:
    """All open ACCREC invoices (excludes PAID/VOIDED/DELETED/DRAFT). Paged."""
    out: list[dict[str, Any]] = []
    where = (
        'Type=="ACCREC" AND Status!="PAID" AND Status!="VOIDED" '
        'AND Status!="DELETED" AND Status!="DRAFT"'
    )
    for page in range(1, 50):
        data = _get(
            entity, "Invoices",
            params={"page": page},
            where=where, order="DueDate ASC",
        )
        batch = data.get("Invoices") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def list_sales_payments_since(entity: str, since_iso: str) -> list[dict[str, Any]]:
    """ACCRECPAYMENTs modified since `since_iso`. Paged."""
    out: list[dict[str, Any]] = []
    for page in range(1, 50):
        data = _get(
            entity, "Payments",
            params={"page": page},
            where='PaymentType=="ACCRECPAYMENT"',
            if_modified_since=since_iso,
        )
        batch = data.get("Payments") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def list_sales_credit_notes(entity: str) -> list[dict[str, Any]]:
    """ACCREC credit notes."""
    data = _get(entity, "CreditNotes", where='Type=="ACCRECCREDIT"')
    return list(data.get("CreditNotes") or [])


def list_customer_contacts(entity: str) -> list[dict[str, Any]]:
    """Customer contacts. Paged."""
    out: list[dict[str, Any]] = []
    for page in range(1, 50):
        data = _get(
            entity, "Contacts",
            params={"page": page},
            where="IsCustomer==true",
        )
        batch = data.get("Contacts") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


# ── parsing utilities ────────────────────────────────────────────────

_XERO_DATE_RE = re.compile(r"/Date\((\d+)([+-]\d{4})?\)/")


def parse_xero_date(value: Any) -> _dt.datetime | None:
    """Xero serialises dates as `/Date(epoch_ms+0000)/` or ISO strings."""
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value
    s = str(value)
    m = _XERO_DATE_RE.match(s)
    if m:
        try:
            return _dt.datetime.fromtimestamp(
                int(m.group(1)) / 1000, tz=_dt.timezone.utc
            )
        except (ValueError, OverflowError):
            return None
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def masked_ref(name: str | None, contact_id: str | None) -> str:
    """Initials + last-4 of contact id. Keeps general reports name-light
    even though receivables doesn't flag people.
    """
    parts = [p for p in (name or "").split() if p]
    initials = "".join(p[0] for p in parts).upper()[:4] or "??"
    tail = (contact_id or "")[-4:] or "????"
    return f"{initials}-{tail}"
