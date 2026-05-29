"""SoD (segregation of duties) detector — STUB.

Phase 3 of the legacy controls-audit spec. Detectors planned:
  - created-vendor-and-approved-payment-to-it
  - raised-invoice-and-applied-its-payment
  - created-and-posted-journal
  - user-with-overbroad-role

Blocked on JBC-confirm C4: real approval limits + an authority map
mapping Xero users → JBC roles → spend caps. Until that lands, this
detector returns [] and the namespace `sod-violation` is reserved.

When wired, findings here will set is_people_flag = True (they name
internal staff), use the '<initials>-XXXX' masked title form, and put
the full name in evidence.individualName per the SKILL's people-flag
invariants.
"""

from __future__ import annotations

from typing import Any


def run_sod(entity: str) -> list[dict[str, Any]]:  # noqa: ARG001
    return []
