"""Read-only Xero OAuth2 client_credentials helper for jbc-receivables.

Two tenants — SC and CQ — selected by the `entity` argument ("SC" | "CQ").
Env vars per tenant: XERO_<entity>_CLIENT_ID / _CLIENT_SECRET / _TENANT_ID.

AR-side endpoints only. The scope list is the minimum read scope set.
No write scopes are requested anywhere in this module.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import re
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


def tenant_configured(entity: str) -> bool:
    try:
        _creds(entity)
        return True
    except RuntimeError:
        return False


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


def _get(
    entity: str,
    path: str,
    params: dict[str, Any] | None = None,
    where: str | None = None,
    order: str | None = None,
    if_modified_since: str | None = None,
) -> dict[str, Any]:
    creds = _creds(entity)

    qp = dict(params or {})
    if where:
        qp["where"] = where
    if order:
        qp["order"] = order
    qs = ("?" + urllib.parse.urlencode(qp, quote_via=urllib.parse.quote)) if qp else ""

    headers = {
        "Xero-Tenant-Id": creds["tenant_id"],
        "Accept": "application/json",
    }
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since

    for attempt in range(3):
        tok = _token(entity)
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
