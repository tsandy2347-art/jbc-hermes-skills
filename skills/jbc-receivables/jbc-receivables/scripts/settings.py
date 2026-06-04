# Specialist settings reader — shared across all jbc-* skills.
#
# Each specialist's tunable thresholds live in the `specialist_settings`
# table on the shared findings DB. The detector reads its row(s) at run
# start, with a fallback to env vars (legacy) and then hard-coded defaults
# so a missing row never crashes the run.
#
# Read-only. Mark writes through a dedicated change tool.

from __future__ import annotations
import os
from typing import Any


def _connect():
    """Same dual-driver connect as the detectors."""
    url = os.environ.get("JBC_FINDINGS_DATABASE_URL") or os.environ.get(
        "HERMES_FINDINGS_DATABASE_URL"
    )
    if not url:
        return None
    try:
        import psycopg  # v3
        return psycopg.connect(url)
    except ImportError:
        import psycopg2
        return psycopg2.connect(url)


def load(specialist: str) -> dict[str, str]:
    """Return {key: value} for the named specialist, or {} on any failure.

    Detectors call this once at the top of their run. Missing keys are NOT
    an error — the caller handles defaults.
    """
    conn = _connect()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM specialist_settings WHERE specialist = %s",
                (specialist,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        # Table missing on a fresh DB, or any transient issue → fall back
        # to env / hardcoded defaults silently. The detector keeps running.
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_int(settings: dict[str, str], key: str, env_name: str, default: int) -> int:
    """Resolution order: settings table → env var → hard-coded default."""
    if key in settings:
        try:
            return int(settings[key])
        except ValueError:
            pass
    raw = os.environ.get(env_name)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return default


def get_float(settings: dict[str, str], key: str, env_name: str, default: float) -> float:
    if key in settings:
        try:
            return float(settings[key])
        except ValueError:
            pass
    raw = os.environ.get(env_name)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default
