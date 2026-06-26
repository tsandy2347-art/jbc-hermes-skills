"""MYOB Pay Activity Summary reader (CSV or XLSX).

The skill is read-only and runs even when the export is missing/unreadable —
callers inspect ``LoadResult.missing`` / ``LoadResult.error`` and emit an
``ingest``-domain finding.

Real MYOB Acumatica "Pay Activity Summary" export (the format Mark uploads) is
an XLSX with these columns:
    Company ID, Branch ID, Branch Name, Payroll Tax State, Employee Tax Status,
    Payment Tax Status, Payroll Tax Category, Value, Employee ID, Employee Name,
    Pay Run ID, Physical pay day, Pay Group ID, Pay Item ID, Pay Item Description
Branch ID is the entity: SC, CQ, and WB (Wide Bay, Location-tracked under the
SC entity → folded to SC here). There is NO hours column — hours/shift-based
detectors (award, overtime, rostering) must come from the AlayaCare feed; MYOB
drives the dollar/labour-cost detectors.

XLSX is parsed with stdlib zipfile+ElementTree on purpose: openpyxl chokes on
MYOB/Acumatica's non-standard stylesheet ("Fill() takes no arguments"), and the
stdlib path needs no extra dependency. We read shared + inline strings and leave
numbers as their stored text form.
"""

from __future__ import annotations

import csv
import datetime as _dt
import os
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

# Excel's 1900 date system, using 1899-12-30 as the epoch so the spurious
# 1900-leap-year bug cancels out for any real date.
_EXCEL_EPOCH = _dt.date(1899, 12, 30)


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
    error: str | None = None


_HEADER_ALIASES = {
    "entitycode": "entity_code", "entity": "entity_code",
    "branchid": "entity_code",                      # MYOB Pay Activity
    "employeeid": "employee_id", "empid": "employee_id",
    "employeename": "employee_name", "name": "employee_name",
    "payrunid": "pay_run_id", "payrun": "pay_run_id",
    "periodstart": "period_start", "periodstartdate": "period_start",
    "periodend": "period_end", "periodenddate": "period_end",
    "physicalpayday": "period_end",                 # MYOB pay date
    "linetype": "line_type", "type": "line_type",
    "payrolltaxcategory": "line_type",              # Wages/Super/Allowances/Term
    "amount": "amount", "amountaud": "amount",
    "value": "amount",                              # MYOB $ column
    "classification": "classification", "class": "classification",
    "payitemdescription": "classification",         # MYOB granular pay item
    "employmenttype": "employment_type", "emptype": "employment_type",
    "hours": "hours",
    "shiftref": "shift_ref", "shiftid": "shift_ref",
    "startts": "start_ts", "start": "start_ts",
    "endts": "end_ts", "end": "end_ts",
}

_XL = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _to_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    s = str(s).replace(",", "").replace("$", "").strip()
    if s in ("-", ""):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def _excel_date(s: str) -> str:
    # XLSX date cells arrive as serial numbers (e.g. "46174"). Convert plausible
    # serials to ISO dates; pass through anything already a date string.
    s = (s or "").strip()
    if not s:
        return ""
    try:
        n = float(s)
    except ValueError:
        return s
    if 20000 <= n <= 80000:  # ~1954..2119 — a real pay date, not a count
        return (_EXCEL_EPOCH + _dt.timedelta(days=int(n))).isoformat()
    return s


def _cell(v: Any) -> str:
    # csv.DictReader stuffs overflow columns (a row with more fields than
    # headers) into a list under the restkey; join those rather than crashing
    # on list.strip(). Normal cells are str/None.
    if isinstance(v, list):
        v = " ".join(x for x in v if x)
    return ("" if v is None else str(v)).strip()


def _entity_from_branch(branch: str) -> str | None:
    b = (branch or "").upper()
    if b in ("SC", "CQ"):
        return b
    if b == "WB":                       # Wide Bay → Location under SC entity
        return "SC"
    low = b.lower()
    if "queen" in low or b.startswith("CQ"):
        return "CQ"
    if b.startswith("SC") or "sunshine" in low or "wide bay" in low:
        return "SC"
    return None


def _detect_encoding(path: str) -> str:
    # MYOB Acumatica CSVs are Windows-1252 (cp1252); older/manual exports are
    # UTF-8 (often with BOM). Forcing utf-8 crashed on bytes like 0xe8 (è).
    # Try the likely encodings in order; latin-1 is the never-fails floor.
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, "r", encoding=enc) as fh:
                fh.read()
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _csv_rows(path: str) -> tuple[list[str], list[dict[str, Any]]]:
    with open(path, "r", newline="", encoding=_detect_encoding(path)) as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = [{k: v for k, v in r.items() if k is not None} for r in reader]
    return header, rows


def _col_index(ref: str) -> int:
    letters = "".join(ch for ch in (ref or "") if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1 if n else 0


def _xlsx_rows(path: str) -> tuple[list[str], list[dict[str, Any]]]:
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        st = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in st.findall(_XL + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_XL + "t")))

    sheets = sorted(n for n in z.namelist()
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    root = ET.fromstring(z.read(sheets[0]))
    data = root.find(_XL + "sheetData")
    row_els = data.findall(_XL + "row") if data is not None else []

    def cell_value(c) -> str:
        t = c.get("t")
        v = c.find(_XL + "v")
        if t == "s" and v is not None and v.text is not None:
            try:
                return shared[int(v.text)]
            except (ValueError, IndexError):
                return ""
        if t == "inlineStr":
            is_el = c.find(_XL + "is")
            if is_el is not None:
                return "".join(x.text or "" for x in is_el.iter(_XL + "t"))
        return v.text if v is not None and v.text is not None else ""

    def as_cells(row_el) -> list[str]:
        # Honour the cell ref so sparse/blank cells don't shift columns.
        by_idx: dict[int, str] = {}
        for c in row_el.findall(_XL + "c"):
            by_idx[_col_index(c.get("r", ""))] = cell_value(c)
        width = (max(by_idx) + 1) if by_idx else 0
        return [by_idx.get(i, "") for i in range(width)]

    header: list[str] | None = None
    rows: list[dict[str, Any]] = []
    for row_el in row_els:
        cells = as_cells(row_el)
        if header is None:
            if any((c or "").strip() for c in cells):
                header = [(c or "").strip() for c in cells]
            continue
        if not any((c or "").strip() for c in cells):
            continue
        rows.append({header[i]: (cells[i] if i < len(cells) else "")
                     for i in range(len(header))})
    return (header or []), rows


def load(path: str) -> LoadResult:
    if not path or not os.path.exists(path):
        return LoadResult(path=path or "", missing=True, lines=[])

    try:
        # MYOB Pay Activity exports as XLSX (a zip); CSV is the legacy/manual path.
        if zipfile.is_zipfile(path):
            header, raw_rows = _xlsx_rows(path)
        else:
            header, raw_rows = _csv_rows(path)
    except Exception as exc:  # noqa: BLE001 - fail loud, never silent 0-rows
        return LoadResult(
            path=path, missing=False, lines=[],
            error=f"could not parse export: {type(exc).__name__}: {exc}",
        )

    canonical = {h: _HEADER_ALIASES.get(_norm(h), _norm(h)) for h in header}

    lines: list[PayLine] = []
    for raw in raw_rows:
        row = {canonical.get(k, _norm(str(k))): _cell(v) for k, v in raw.items()}
        entity = _entity_from_branch(row.get("entity_code") or "")
        if entity is None:
            continue
        lines.append(PayLine(
            entity_code=entity,
            employee_id=row.get("employee_id") or "",
            employee_name=row.get("employee_name") or "",
            pay_run_id=row.get("pay_run_id") or "",
            period_start=_excel_date(row.get("period_start") or ""),
            period_end=_excel_date(row.get("period_end") or ""),
            line_type=(row.get("line_type") or "").lower(),
            amount=_to_float(row.get("amount")) or 0.0,
            classification=row.get("classification") or None,
            employment_type=(row.get("employment_type") or None),
            hours=_to_float(row.get("hours")),
            shift_ref=row.get("shift_ref") or None,
            start_ts=row.get("start_ts") or None,
            end_ts=row.get("end_ts") or None,
            raw={**row, "branch_id": (raw.get("Branch ID") or row.get("entity_code") or "")},
        ))

    return LoadResult(path=path, missing=False, lines=lines)
