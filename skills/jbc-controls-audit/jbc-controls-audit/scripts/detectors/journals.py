"""Journal-integrity (named-author) detector — STUB.

Phase 4 of the legacy controls-audit spec. Detectors planned:
  - manual journal posted to a rarely-used account
  - round-number manual journal
  - period-end manual journal
  - per-user journal spike

Blocked on: requires linking ManualJournal.UpdatedByUserID to the
Users roster (Phase 2 already pulls Users) and a learned baseline
of per-user / per-account journal counts. Will be authored once the
elevated-user-roster is stable in the DB.

When wired, named-author findings here will set is_people_flag = True
and apply the '<initials>-XXXX' title-mask invariant.

NOTE: this detector group is the *named-author* slice. The systemic
journal-anomaly slice (late posting, large posting, unposted) is
handled by `jbc-reconciliation`'s journal detector, not here.
"""

from __future__ import annotations

from typing import Any


def run_journals(entity: str) -> list[dict[str, Any]]:  # noqa: ARG001
    return []
