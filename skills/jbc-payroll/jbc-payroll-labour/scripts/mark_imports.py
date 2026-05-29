"""Mark imports fetcher — pulls the latest uploaded CSV from Mark's
/api/imports/<kind>/latest endpoint into a local temp file.

The skills that consume CSVs (jbc-payroll-labour for MYOB, jbc-revenue-claims
for AlayaCare) call ``fetch_latest_to_tempfile(kind)`` BEFORE their existing
local-file load(). When MARK_IMPORT_BASE_URL is set in the environment,
this hits Mark and writes the bytes to a tempfile, returning the path.
When MARK_IMPORT_BASE_URL is unset, this returns None and the caller falls
back to whatever local path is configured (legacy behaviour).

Auth: sends the value of MARK_IMPORT_AUTH as the Authorization header
(e.g. "Basic base64(user:pass)" — the same gate Tony/Lindsay log in with).

Errors that DON'T raise:
  - MARK_IMPORT_BASE_URL unset → return None (legacy local-path mode)
  - 404 from Mark (no upload yet) → return None (caller emits the
    standard *-export-missing ingest finding)

Errors that DO raise (caller's try/except will turn this into an
ingest-domain ``ingest-failure`` finding):
  - 401 / 403 (auth misconfigured)
  - 5xx / network failure
"""
from __future__ import annotations

import os
import tempfile
import urllib.request
import urllib.error

# kind → Mark endpoint path
_ENDPOINTS = {
    "myob": "/api/imports/myob/latest",
    "alayacare": "/api/imports/alayacare/latest",
}


def configured() -> bool:
    return bool(os.environ.get("MARK_IMPORT_BASE_URL"))


def fetch_latest_to_tempfile(kind: str) -> str | None:
    """Fetch the latest upload of ``kind`` from Mark and write to a tempfile.

    Returns the tempfile path on success, or None when:
      - MARK_IMPORT_BASE_URL is unset (legacy mode)
      - Mark returns 404 (no upload of this kind yet)
    Raises on auth or transport failure.
    """
    base = os.environ.get("MARK_IMPORT_BASE_URL", "").rstrip("/")
    if not base:
        return None
    path = _ENDPOINTS.get(kind)
    if not path:
        raise ValueError(f"unknown import kind: {kind!r}")
    url = base + path
    auth = os.environ.get("MARK_IMPORT_AUTH", "")
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", auth)
    req.add_header("User-Agent", "jbc-hermes-skill")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            # Honour Mark's filename header for nicer suffix-matching downstream
            disp = resp.headers.get("Content-Disposition", "")
            suffix = ".csv"
            if ".pdf" in disp.lower():
                suffix = ".pdf"
            elif ".xlsx" in disp.lower():
                suffix = ".xlsx"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    fd, tmp_path = tempfile.mkstemp(prefix=f"mark-import-{kind}-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except Exception:
        os.unlink(tmp_path)
        raise
    return tmp_path
