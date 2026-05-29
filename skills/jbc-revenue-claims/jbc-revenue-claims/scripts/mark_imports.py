"""See jbc-payroll-labour/scripts/mark_imports.py for full docs.
Identical module — duplicated per-skill because Hermes skills don't share a
Python path between bundles. If you patch one, patch the other.
"""
from __future__ import annotations

import os
import tempfile
import urllib.request
import urllib.error

_ENDPOINTS = {
    "myob": "/api/imports/myob/latest",
    "alayacare": "/api/imports/alayacare/latest",
}


def configured() -> bool:
    return bool(os.environ.get("MARK_IMPORT_BASE_URL"))


def fetch_latest_to_tempfile(kind: str) -> str | None:
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
