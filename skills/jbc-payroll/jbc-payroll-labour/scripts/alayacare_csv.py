"""AlayaCare timesheet CSV reader. Mirrors myob_csv shape."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Shift:
    entity_code: str
    employee_id: str
    employee_name: str
    shift_id: str
    start_ts: str
    end_ts: str
    billable_hours: float
    paid_hours: float
    shift_kind: str
    pay_run_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadResult:
    path: str
    missing: bool
    shifts: list[Shift]


_HEADER_ALIASES = {
    "entitycode": "entity_code",
    "employeeid": "employee_id",
    "employeename": "employee_name",
    "shiftid": "shift_id",
    "startts": "start_ts", "start": "start_ts",
    "endts": "end_ts", "end": "end_ts",
    "billablehours": "billable_hours",
    "paidhours": "paid_hours",
    "shiftkind": "shift_kind", "kind": "shift_kind",
    "payrunid": "pay_run_id",
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _to_float(s: str | None) -> float:
    if s is None or s == "":
        return 0.0
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def load(path: str) -> LoadResult:
    if not path or not os.path.exists(path):
        return LoadResult(path=path or "", missing=True, shifts=[])
    shifts: list[Shift] = []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        canonical: dict[str, str] = {}
        for h in reader.fieldnames or []:
            n = _norm(h)
            canonical[h] = _HEADER_ALIASES.get(n, n)
        for raw_row in reader:
            row = {canonical.get(k, k): (v or "").strip() for k, v in raw_row.items()}
            entity = (row.get("entity_code") or "").upper()
            if entity not in ("SC", "CQ"):
                if "queen" in entity.lower() or entity.startswith("CQ"):
                    entity = "CQ"
                elif entity.startswith("SC") or "sunshine" in entity.lower():
                    entity = "SC"
                else:
                    continue
            shifts.append(Shift(
                entity_code=entity,
                employee_id=row.get("employee_id") or "",
                employee_name=row.get("employee_name") or "",
                shift_id=row.get("shift_id") or "",
                start_ts=row.get("start_ts") or "",
                end_ts=row.get("end_ts") or "",
                billable_hours=_to_float(row.get("billable_hours")),
                paid_hours=_to_float(row.get("paid_hours")),
                shift_kind=(row.get("shift_kind") or "").lower(),
                pay_run_id=row.get("pay_run_id") or None,
                raw=row,
            ))
    return LoadResult(path=path, missing=False, shifts=shifts)
