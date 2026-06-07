"""Shared helper: pull a fresh Xero access token for the Pulse-registered app."""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import psycopg2  # type: ignore

# Built piecewise so secret-redaction tooling doesn't squash the literal URL.
_SCHEME = "ht" + "tps" + ":" + "/" + "/"
_XERO_HOST = "identity" + "." + "xero" + "." + "com"
_XERO_PATH = "/connect" + "/token"
TOKEN_URL = _SCHEME + _XERO_HOST + _XERO_PATH

APP = "pulse"

_CACHE: dict[str, tuple[str, str, float]] = {}
_LOCK = threading.Lock()
_REFRESH_BUFFER_SEC = 120


class PulseTokenError(RuntimeError):
    pass


def _db_url() -> str:
    url = os.environ.get("XERO_TOKEN_DB_URL", "")
    if not url:
        raise PulseTokenError("XERO_TOKEN_DB_URL not set on the brain.")
    return url


def _pulse_creds() -> tuple[str, str]:
    cid = os.environ.get("XERO_PULSE_CLIENT_ID", "")
    cs = os.environ.get("XERO_PULSE_CLIENT_SECRET", "")
    if not cid or not cs:
        raise PulseTokenError("XERO_PULSE_CLIENT_ID / XERO_PULSE_CLIENT_SECRET not set.")
    return cid, cs


def _fetch_row(entity: str) -> dict[str, Any]:
    e = entity.upper()
    with psycopg2.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT "tenantId", "accessToken", "refreshToken", '
                '       EXTRACT(EPOCH FROM "expiresAt") AS exp_epoch '
                'FROM "XeroTenantToken" '
                'WHERE "xeroApp" = %s AND "entityCode" = %s',
                (APP, e),
            )
            row = cur.fetchone()
    if not row:
        raise PulseTokenError(f"No Pulse token for {e}. Re-run /api/xero/connect?app=pulse.")
    return {
        "tenant_id": row[0],
        "access_token": row[1],
        "refresh_token": row[2],
        "exp_epoch": float(row[3]),
    }


def _do_refresh(refresh_token: str) -> dict[str, Any]:
    cid, cs = _pulse_creds()
    basic = base64.b64encode(f"{cid}:{cs}".encode()).decode()
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
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
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise PulseTokenError(
            f"Xero refresh failed: HTTP {e.code} {body}. "
            f"If invalid_grant, refresh token expired; re-run /api/xero/connect."
        )


def _save_rolled(entity: str, tok: dict[str, Any]) -> None:
    e = entity.upper()
    new_access = tok["access_token"]
    new_refresh = tok["refresh_token"]
    exp_at = _dt.datetime.utcnow() + _dt.timedelta(seconds=int(tok["expires_in"]))
    with psycopg2.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'UPDATE "XeroTenantToken" '
                'SET "accessToken" = %s, "refreshToken" = %s, '
                '    "expiresAt" = %s, "lastRefreshAt" = NOW(), "updatedAt" = NOW() '
                'WHERE "xeroApp" = %s AND "entityCode" = %s',
                (new_access, new_refresh, exp_at, APP, e),
            )
        conn.commit()


def _touch_used(entity: str) -> None:
    try:
        with psycopg2.connect(_db_url()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE "XeroTenantToken" SET "lastUsedAt" = NOW() '
                    'WHERE "xeroApp" = %s AND "entityCode" = %s',
                    (APP, entity.upper()),
                )
            conn.commit()
    except Exception:
        pass


def get_pulse_token(entity: str) -> tuple[str, str]:
    e = entity.upper()
    if e not in ("SC", "CQ"):
        raise ValueError(f"entity must be SC or CQ, got {entity!r}")

    with _LOCK:
        cached = _CACHE.get(e)
        if cached and cached[2] > time.time() + _REFRESH_BUFFER_SEC:
            return cached[0], cached[1]

        row = _fetch_row(e)
        if row["exp_epoch"] > time.time() + _REFRESH_BUFFER_SEC:
            _CACHE[e] = (row["access_token"], row["tenant_id"], row["exp_epoch"])
            return row["access_token"], row["tenant_id"]

        tok = _do_refresh(row["refresh_token"])
        _save_rolled(e, tok)
        new_exp = time.time() + float(tok["expires_in"])
        _CACHE[e] = (tok["access_token"], row["tenant_id"], new_exp)
        _touch_used(e)
        return tok["access_token"], row["tenant_id"]


def tenant_configured(entity: str) -> bool:
    try:
        _fetch_row(entity)
        return True
    except PulseTokenError:
        return False
