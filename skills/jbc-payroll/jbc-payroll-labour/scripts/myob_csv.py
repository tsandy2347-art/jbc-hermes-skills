"""MYOB Pay Activity Summary CSV reader.

The skill is read-only and runs even when the CSV is missing — callers
inspect ``LoadResult.missing`` and emit an ``ingest``-domain finding.

Expected columns (case-insensitive, hyphens/underscores normalised):
    entity_code, employee_id, employee_name, pay_run_id,
    period_start, period_end, line_type, amount,
    classification (opt), employment_type (opt), hours (opt),
    shift_ref (opt), start_ts (opt), end_ts (opt).
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PayLine:
    entity_code: str
    employee_id: str
    employee_name: str
    pay_run_id: str
    period_start: str
    period_end: str
    line_type: str
    amount: float
    classification: str | None = None
    employment_type: str | None = None
    hours: float | None = None
    shift_ref: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadResult:
    path: str
    missing: bool
    lines: list[PayLine]


_HEADER_ALIASES = {
    "entitycode": "entity_code", "entity": "entity_code",
    "employeeid": "employee_id", "empid": "employee_id",
    "employeename": "employee_name", "name": "employee_name",
    "payrunid": "pay_run_id", "payrun": "pay_run_id",
    "periodstart": "period_start", "periodstartdate": "period_start",
    "periodend": "period_end", "periodenddate": "period_end",
    "linetype": "line_type", "type": "line_type",
    "amount": "amount", "amountaud": "amount",
    "classification": "classification", "class": "classification",
    "employmenttype": "employment_type", "emptype": "employment_type",
    "hours": "hours",
    "shiftref": "shift_ref", "shiftid": "shift_ref",
    "startts": "start_ts", "start": "start_ts",
    "endts": "end_ts", "end": "end_ts",
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _row_get(row: dict[str, str], key: str) -> str | None:
    for k, v in row.items():
        if _norm(k) == _norm(key):
            return v
    alias = _HEADER_ALIASES.get(_norm(key))
    if alias:
        for k, v in row.items():
            if _norm(k) == _norm(alias):
                return v
    return None


def _to_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    s = s.replace(",", "").replace("$", "").strip()
    if s in ("-", ""):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def load(path: str) -> LoadResult:
    if not path or not os.path.exists(path):
        return LoadResult(path=path or "", missing=True, lines=[])

    lines: list[PayLine] = []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        # Normalise headers once.
        canonical: dict[str, str] = {}
        for h in reader.fieldnames or []:
            n = _norm(h)
            canonical[h] = _HEADER_ALIASES.get(n, n)

        for raw_row in reader:
            row = {canonical.get(k, k): (v or "").strip() for k, v in raw_row.items()}
            entity = (row.get("entity_code") or "").upper()
            if entity not in ("SC", "CQ"):
                # MYOB occasional typo "Central Queenland" → CQ heuristic.
                if "queen" in entity.lower() or entity.startswith("CQ"):
                    entity = "CQ"
                elif entity.startswith("SC") or "sunshine" in entity.lower():
                    entity = "SC"
                else:
                    continue
            amount = _to_float(row.get("amount")) or 0.0
            lines.append(PayLine(
                entity_code=entity,
                employee_id=row.get("employee_id") or "",
                employee_name=row.get("employee_name") or "",
                pay_run_id=row.get("pay_run_id") or "",
                period_start=row.get("period_start") or "",
                period_end=row.get("period_end") or "",
                line_type=(row.get("line_type") or "").lower(),
                amount=amount,
                classification=row.get("classification") or None,
                employment_type=(row.get("employment_type") or None),
                hours=_to_float(row.get("hours")),
                shift_ref=row.get("shift_ref") or None,
                start_ts=row.get("start_ts") or None,
                end_ts=row.get("end_ts") or None,
                raw=row,
            ))

    return LoadResult(path=path, missing=False, lines=lines)
