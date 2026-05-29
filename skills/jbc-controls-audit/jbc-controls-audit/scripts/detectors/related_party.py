"""Related-party detector — STUB.

Phase 4 of the legacy controls-audit spec. Detectors planned:
  - vendor-bank == employee-bank
  - shared surname / address / phone between vendor and staff
  - new-hire added by manager whose details partially match

Blocked on:
  - JBC-confirm C2 — MYOB access mode (no API today)
  - JBC-confirm C3 — AlayaCare staff/bank CSV export (neither
    claire-agent nor payroll-analyser currently export bank fields)

Until both data sources land, this detector returns [] and the
namespace `related-party-flag` is reserved.

When wired, findings here will set is_people_flag = True, mask
individual names to '<initials>-XXXX' in the title, and put the
full name in evidence.individualName + evidence.isRestricted = True
per the SKILL's people-flag invariants.
"""

from __future__ import annotations

from typing import Any


def run_related_party(entity: str) -> list[dict[str, Any]]:  # noqa: ARG001
    return []
