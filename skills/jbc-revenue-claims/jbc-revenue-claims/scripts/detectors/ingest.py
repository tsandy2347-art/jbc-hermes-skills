"""Ingest + plumbing detectors.

- alayacare-export-missing: emitted when ALAYACARE_SERVICE_EXPORT is
  unset OR the file does not exist. Single ingest-domain finding,
  entity_code='consolidated' (not statutory), is_people_flag=false.
- channel-not-configured: emitted per program when the claim-channel
  env vars are unset. Blocks the (un-ported) submission domain;
  we still surface it so Mark knows.
"""

from __future__ import annotations

import os
from typing import Any


def alayacare_missing_finding() -> dict[str, Any]:
    return {
        "detector": "alayacare-export-missing",
        "domain": "ingest",
        "severity": "warning",
        "entity_code": "consolidated",
        "is_people_flag": False,
        "title": "AlayaCare service export not available",
        "detail": (
            "ALAYACARE_SERVICE_EXPORT is unset or the CSV does not exist. "
            "AlayaCare has no usable API for delivered-service data — the skill "
            "needs the CSV drop to detect unclaimed services, evidence gaps and "
            "budget exhaustion. No revenue-side findings will be produced this "
            "run beyond plumbing visibility."
        ),
        "evidence": {
            "dedupKey": "alayacare-export-missing",
            "kind": "ingest-gap",
            "envVar": "ALAYACARE_SERVICE_EXPORT",
            "envValue": os.environ.get("ALAYACARE_SERVICE_EXPORT", ""),
        },
    }


def channel_not_configured() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for program, var in (("NDIS", "NDIS_CLAIM_CHANNEL"),
                         ("SAH", "SAH_CLAIM_CHANNEL")):
        val = (os.environ.get(var) or "").strip().lower()
        if val in ("api", "bulk-upload", "manual"):
            continue
        out.append({
            "detector": "channel-not-configured",
            "domain": "channel",
            "severity": "info",
            "entity_code": "consolidated",
            "is_people_flag": False,
            "title": f"{program} claim channel not configured",
            "detail": (
                f"{var} is unset (or not one of api|bulk-upload|manual). "
                "Submission domain is gated on this — the skill is read-only "
                "today, but Mark should surface the gap so it can be wired."
            ),
            "evidence": {
                "dedupKey": f"channel-not-configured:{program}",
                "program": program,
                "envVar": var,
                "envValue": os.environ.get(var, ""),
            },
        })
    return out
