"""parse_myob_payroll.py — read MYOB exports → journal lines for create-payroll-journal.

Inputs (any one pay period):
  1. Pay Activity Summary .xlsx (hierarchical RUN→BRANCH→DEPT→…→EMPLOYEE)
     → gross/PAYG/super/net at branch level (for the payable CRs)
  2. Pay Activity Detail Data .xlsx (flat tabular, ONE row per pay-item-per-employee)
     → fallback for amount totals; gives employee → primary cost-centre map
  3. Pay Activity Detail Report .xlsx (hierarchical, per-line w/ GL+SubAccount)
     → THE SOURCE OF TRUTH for which expense GL each line posts to

CRITICAL CALIBRATION (against Journal #673782 / PAY-001910):
  Craig posts to Xero from MYOB sections: Gross Income, Tax Free Income, Employer Superannuation
  Craig does NOT post Entitlement Accrual (those are info-only liability movements)
  Craig DOES post these specific Entitlement PAYMENTS:
    - Annual Leave Taken    → DR 918  Provision for Annual Leave (reduces liability)
    - Personal Leave Taken  → DR 477.7 Sick Leave
    - Leave Loading Expense → DR 477.6 Vacation Leave (Income section, not Accrual)

Tax Free Income lines without a GL stamp (travel allowances, sleepover super)
fall through to the employee's primary cost-centre's wages account.

The Pay Activity Detail Report stamps GL on most lines:
  Section "Gross Income"             → col 33 = "477-Wages and Salaries - Direct" etc.
  Section "Employer Superannuation"  → col 32 = "478-Superannuation - Direct"
  Section "Entitlement Accrual"      → col 34 (ignored — accrual is info-only)
GL string continues onto next row in same column.
Sub-account: col 44 for Gross/TaxFree, col 41 for Super.

USAGE:
    python3 parse_myob_payroll.py <summary.xlsx> <data.xlsx> <detail_report.xlsx> [pay-run-id]
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
SMNS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'

# Branch code → Xero Location tracking option (set by configuration)
BRANCH_BY_SUB_PREFIX = {'SC': 'SC', 'WB': 'WB', 'CQ': 'CQ'}

# Sections in MYOB Detail Report and whether they post to Xero
SECTIONS = ('Gross Income', 'Tax Free Income', 'Pre-tax Deductions',
            'Employer Superannuation', 'Entitlement Accrual',
            'Deductions', 'Net Pay', 'After-tax Income')
POSTING_SECTIONS = ('Gross Income', 'Tax Free Income', 'Employer Superannuation')

SUB_PATTERN = re.compile(r'^[A-Z]{2}\d{2}-[A-Z]{2}-')


def find_sub_in_row(row):
    """Scan all cells in a row, return first that matches sub-account pattern."""
    for v in row.values():
        if v and isinstance(v, str):
            s = v.strip()
            if SUB_PATTERN.match(s):
                return s
    return ''


def find_gl_in_row(row):
    """Scan all cells in a row for an account code pattern like '477-…', '918-…', '478.1-…'."""
    for v in row.values():
        if v and isinstance(v, str):
            s = v.strip()
            m = re.match(r'^(\d{3,4}(?:\.\d+)?)\s*-\s*(.+)', s)
            if m:
                return s, m.group(1)
    return '', ''


def find_gl_continuation(row):
    """Find a GL continuation fragment (no leading digits)."""
    for v in row.values():
        if v and isinstance(v, str):
            s = v.strip()
            # Continuation like " Direct" or " Indirect" — short, no digits
            if s and not s[0].isdigit() and len(s) < 30 and not SUB_PATTERN.match(s):
                # heuristic: very short text fragments that complement a GL name
                if any(w in s for w in ('Direct', 'Indirect', 'Salaries', 'Leave', 'Clearing')):
                    return s
    return ''


# Column positions in Pay Activity Detail Report (1-indexed)
# Used as hints/fallback only — primary lookup is scan-by-pattern
GL_COL = {
    'Gross Income': 33,
    'Tax Free Income': 33,
    'Employer Superannuation': 32,
    'Entitlement Accrual': 34,  # for completeness — not posted
}
SUB_COL = {
    'Gross Income': 44,
    'Tax Free Income': 44,
    'Employer Superannuation': 41,
    'Entitlement Accrual': 41,
}

# Leave-payment lines (paid out, reclassified to leave accounts)
LEAVE_PAYMENT_RULES = {
    'Annual Leave Taken':    {'code': '918',   'name': 'Provision for Annual Leave'},
    'Personal Leave Taken':  {'code': '477.7', 'name': 'Sick Leave'},
    'Leave Loading Expense': {'code': '477.6', 'name': 'Vacation Leave'},
}


def _col_idx(ref):
    s = ''.join(c for c in ref if c.isalpha())
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n


def _load_rows(path):
    z = zipfile.ZipFile(path)
    ss = []
    for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si', NS):
        ss.append(''.join(t.text or '' for t in si.iter(SMNS + 't')))
    sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
    rows = {}
    for row in sheet.iter(SMNS + 'row'):
        r = int(row.attrib['r'])
        cells = {}
        for c in row:
            ref = c.attrib.get('r', '')
            ci = _col_idx(ref)
            t = c.attrib.get('t', '')
            v = c.find('s:v', NS)
            is_ = c.find('s:is', NS)
            val = None
            if t == 's' and v is not None:
                val = ss[int(v.text)]
            elif t == 'inlineStr' and is_ is not None:
                val = ''.join(x.text or '' for x in is_.iter(SMNS + 't'))
            elif v is not None:
                val = v.text
            cells[ci] = val
        rows[r] = cells
    return rows


def _load_tabular(path):
    rows = _load_rows(path)
    hdr_row = rows[1]
    hdr = {hdr_row.get(i): i for i in hdr_row}
    return [{k: rows[r].get(i) for k, i in hdr.items()}
            for r in range(2, max(rows) + 1)]


def _num(v):
    if v is None or v == '':
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _excel_to_iso(v):
    if v is None or v == '':
        return ''
    s = str(v).strip()
    if re.match(r'\d{4}-\d{1,2}-\d{1,2}', s):
        return s
    if re.match(r'\d{1,2}/\d{1,2}/\d{4}', s):
        d, m, y = s.split('/')
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    try:
        serial = int(float(s))
    except (ValueError, TypeError):
        return s
    import datetime as _dt
    return (_dt.date(1899, 12, 30) + _dt.timedelta(days=serial)).isoformat()


# ── 1. Summary → branch-level cash/payable totals ─────────────────────────────

def parse_summary(path):
    """Walk Pay Activity Summary hierarchy. Accumulate at DEPT row."""
    rows = _load_rows(path)
    by_branch = defaultdict(lambda: {
        'gross': 0.0, 'pretax_ded': 0.0, 'payg': 0.0, 'after_tax': 0.0,
        'post_tax_ded': 0.0, 'net': 0.0, 'employer_super': 0.0,
    })
    meta = {}
    current_branch = None
    BRANCH_NORM = {
        'sunshine coast': 'SC', 'wide bay': 'WB',
        'central queenland': 'CQ', 'central queensland': 'CQ',
    }

    for r in sorted(rows):
        row = rows[r]
        a = row.get(1) or ''
        b = row.get(2) or ''
        c = row.get(3) or ''
        if isinstance(a, str):
            if a.startswith('Physical Pay Date From'):
                meta['from'] = str(row.get(2) or '')
                continue
            if a.startswith('Physical Pay Date To'):
                meta['to'] = str(row.get(2) or '')
                continue
        if a:
            current_branch = None
            continue
        if b:
            current_branch = BRANCH_NORM.get(re.sub(r'\s+', ' ', str(b).strip()).lower())
            continue
        if c and current_branch:
            by_branch[current_branch]['gross']          += _num(row.get(6))
            by_branch[current_branch]['pretax_ded']     += _num(row.get(7))
            by_branch[current_branch]['payg']           += _num(row.get(9))
            by_branch[current_branch]['after_tax']      += _num(row.get(10))
            by_branch[current_branch]['post_tax_ded']   += _num(row.get(11))
            by_branch[current_branch]['net']            += _num(row.get(12))
            by_branch[current_branch]['employer_super'] += _num(row.get(14))

    for k in by_branch:
        for f in by_branch[k]:
            by_branch[k][f] = round(by_branch[k][f], 2)
    return dict(by_branch), meta


# ── 2. Data export → employee → primary cost-centre map ───────────────────────
# Plus leave payment lines (taken-leave reclass).

def parse_data(path):
    return _load_tabular(path)


def build_emp_sub_map(detail_path, target_run=None):
    """Per-employee primary sub-account.
    Strategy: walk the file. For each PAY-NNNN data row, capture the
    sub-account if present + remember the current employee context.
    The Employer Superannuation row always carries a sub-account; use
    THAT as the canonical employee sub-account.
    If target_run is given, only use rows from that pay run.
    """
    rows = _load_rows(detail_path)
    current_emp = None
    emp_sub = {}
    emp_sub_super = {}  # specifically from Super lines — highest signal
    current_section = None
    for r in sorted(rows):
        row = rows[r]
        sec = row.get(4)
        if sec in SECTIONS:
            current_section = sec
        emp_col = row.get(9)
        if emp_col and isinstance(emp_col, str):
            parts = emp_col.split(None, 1)
            if parts and parts[0].isdigit():
                current_emp = parts[0]
        h = row.get(8)
        if h and str(h).startswith('PAY-'):
            if target_run and target_run not in str(h):
                continue
            sub = find_sub_in_row(row) or find_sub_in_row(rows.get(r + 1, {}))
            if sub and current_emp:
                if current_section == 'Employer Superannuation':
                    emp_sub_super[current_emp] = sub
                elif current_emp not in emp_sub:
                    emp_sub[current_emp] = sub
    # Super-row mapping wins
    final = dict(emp_sub)
    final.update(emp_sub_super)
    return final


# ── 3. Detail Report → per-line GL+Sub aggregation (SOURCE OF TRUTH) ──────────

def aggregate_expenses(detail_path, target_runs):
    """Read Pay Activity Detail Report, sum amounts by (GL code, Sub-Account)
    for POSTING_SECTIONS. Lines with no GL stamp are allocated to 477/478
    based on the employee's primary sub-account (from emp_sub map).

    target_runs: iterable of pay run IDs (e.g. ('PAY-001910',)) or
                 ('PAY-001910','PAY-001911','PAY-001912') for adhoc-combined journals.

    Returns:
      gl_agg: dict[(gl_code, sub_account)] → amount
      orphans: list of (section, pay_item, employee_id, amount) for un-allocatable lines
    """
    if isinstance(target_runs, str):
        target_runs = (target_runs,)
    rows = _load_rows(detail_path)
    gl_agg = defaultdict(float)
    orphans = []
    current_section = None
    current_item = None
    current_item_total = 0.0
    current_emp = None

    # Build the emp→sub map inline (super-rows preferred)
    emp_sub_super = {}
    emp_sub_first = {}
    for r in sorted(rows):
        row = rows[r]
        sec = row.get(4)
        if sec in SECTIONS:
            current_section = sec
        emp_col = row.get(9)
        if emp_col and isinstance(emp_col, str):
            parts = emp_col.split(None, 1)
            if parts and parts[0].isdigit():
                current_emp = parts[0]
        h = row.get(8)
        if h and any(run in str(h) for run in target_runs):
            sub_here = find_sub_in_row(row) or find_sub_in_row(rows.get(r + 1, {}))
            if sub_here and current_emp:
                if current_section == 'Employer Superannuation':
                    emp_sub_super[current_emp] = sub_here
                elif current_emp not in emp_sub_first:
                    emp_sub_first[current_emp] = sub_here
    emp_sub = {**emp_sub_first, **emp_sub_super}

    # Reset and do real aggregation
    current_section = None
    current_item = None
    current_item_total = 0.0
    current_emp = None
    for r in sorted(rows):
        row = rows[r]
        sec = row.get(4)
        if sec in SECTIONS:
            current_section = sec
            continue
        emp_col = row.get(9)
        if emp_col and isinstance(emp_col, str):
            parts = emp_col.split(None, 1)
            if parts and parts[0].isdigit():
                current_emp = parts[0]
        label = row.get(5)
        h = row.get(8)
        if label and (not h or not str(h).startswith('PAY-')):
            current_item = label
            current_item_total = _num(row.get(24)) or _num(row.get(25))
            continue
        if h and any(run in str(h) for run in target_runs):
            if current_section not in POSTING_SECTIONS:
                continue
            # GL: scan current row first, then next row
            gl_str, gl_code = find_gl_in_row(row)
            if not gl_str:
                gl_str, gl_code = find_gl_in_row(rows.get(r + 1, {}))
            if gl_str:
                cont = find_gl_continuation(rows.get(r + 1, {}))
                if cont and not gl_str.endswith(cont):
                    gl_str = gl_str + cont
            sub = find_sub_in_row(row) or find_sub_in_row(rows.get(r + 1, {}))
            val = _num(row.get(25)) or current_item_total
            if val == 0:
                continue
            if not gl_code:
                # Allocate orphan to 477 or 478 based on section + employee's primary sub
                sub_for_orphan = sub or emp_sub.get(current_emp or '', '')
                if current_section == 'Employer Superannuation':
                    target_gl = '478'
                else:
                    target_gl = '477'
                if sub_for_orphan:
                    gl_agg[(target_gl, sub_for_orphan)] += val
                else:
                    orphans.append((current_section, current_item, current_emp, val))
            else:
                gl_agg[(gl_code, sub)] += val

    return dict(gl_agg), orphans, emp_sub


def reallocate_orphans(orphans, emp_sub, summary_by_branch):
    """Orphan lines (no GL stamp) — travel allowances, sleepover super, etc.
    Allocate by employee's primary cost-centre, into:
      - Tax Free Income / Gross Income → 477 (Wages — Direct) using emp sub-account
      - Employer Superannuation        → 478 (Super — Direct) using emp sub-account

    This mirrors what we know from Craig's posting pattern: allowances roll
    into the wages account; sleepover super rolls into super.
    """
    reallocated = defaultdict(float)
    unresolved = []
    for sec, item, emp_id, line_sub, amt in orphans:
        # Get sub from emp or fall back to the line's sub if present
        sub = line_sub if (line_sub and '-' in line_sub) else emp_sub.get(emp_id or '', '')
        if not sub:
            unresolved.append((sec, item, emp_id, amt))
            continue
        if sec == 'Employer Superannuation':
            gl_code = '478'
        else:
            gl_code = '477'
        reallocated[(gl_code, sub)] += amt
    return dict(reallocated), unresolved


# ── 4. Build leave-payment reclass adjustments ────────────────────────────────

def build_leave_reclass(data, emp_sub, target_run):
    """Returns dict[(gl_code, sub_account)] → amount for the 3 leave pay items.
    These ADD to the expense GLs (918/477.6/477.7) AND we need to CR the
    matching wages account because the underlying amount is already in Gross
    Income → 477. Net effect: reclass wages to leave accounts.

    Returns:
      additions: dict[(gl_code, sub)] → amount (positive DR to 918/477.6/477.7)
      subtractions: dict[(gl_code, sub)] → amount (negative DR / contra to 477)
    """
    additions = defaultdict(float)
    subtractions = defaultdict(float)  # to be netted against 477/477.4
    audit = []
    for d in data:
        if d.get('Pay Run ID') != target_run:
            continue
        pi = d.get('Pay Item Description')
        if pi not in LEAVE_PAYMENT_RULES:
            continue
        amt = _num(d.get('Amount'))
        if amt == 0:
            continue
        emp_id = str(d.get('Employee ID') or '')
        sub = emp_sub.get(emp_id, 'UNKNOWN')
        rule = LEAVE_PAYMENT_RULES[pi]
        additions[(rule['code'], sub)] += amt
        # Subtract from 477 wages (Direct) at same sub — leave-taken came
        # through Gross Income → 477 originally
        subtractions[('477', sub)] += amt
        audit.append((pi, f"{d.get('First Name')} {d.get('Last Name')}", emp_id, sub, amt))
    return dict(additions), dict(subtractions), audit


def sub_to_branch(sub):
    """Map sub-account prefix to branch code."""
    if not sub:
        return '??'
    prefix = sub[:2].upper()
    return BRANCH_BY_SUB_PREFIX.get(prefix, '??')


def pick_default_run(data):
    counts = defaultdict(int)
    for d in data:
        counts[d.get('Pay Run ID')] += 1
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    summary_path = sys.argv[1]
    data_path = sys.argv[2]
    detail_path = sys.argv[3]
    target_run = sys.argv[4] if len(sys.argv) > 4 else None

    print("=== Loading MYOB exports ===")
    by_branch, meta = parse_summary(summary_path)
    print(f"  Pay Activity Summary: {len(by_branch)} branches")
    if meta:
        print(f"  Physical pay-date range: {_excel_to_iso(meta.get('from','?'))} → {_excel_to_iso(meta.get('to','?'))}")

    data = parse_data(data_path)
    print(f"  Pay Activity Detail Data: {len(data)} rows")

    if target_run is None:
        target_run = pick_default_run(data)
        print(f"  Auto-selected pay run: {target_run}")

    # Allow combining multiple pay runs into one journal (Craig's pattern for SC tenant
    # which can include weekly + adhoc-correction runs)
    target_runs = target_run if isinstance(target_run, (list, tuple)) else (target_run,)
    if isinstance(target_run, str) and ',' in target_run:
        target_runs = tuple(s.strip() for s in target_run.split(','))
    print(f"  Pay runs to aggregate: {', '.join(target_runs)}")

    # 1. GL aggregation from Detail Report (with inline orphan reallocation)
    gl_agg, orphans, emp_sub = aggregate_expenses(detail_path, target_runs)
    print(f"\n=== Detail Report aggregation ===")
    print(f"  {len(emp_sub)} employee→sub-account mappings (super-row preferred)")
    print(f"  {len(gl_agg)} (GL, Sub) buckets")
    if orphans:
        print(f"  WARNING: {len(orphans)} fully unresolved orphans:")
        for sec, item, emp, amt in orphans[:5]:
            print(f"    {sec} | {item} | emp={emp} | ${amt:.2f}")

    # 2. Leave-payment cross-check (from Data export) — Leave Loading Expense
    # is not stamped in the Detail Report. Add it to 477.6 if missing.
    additions, _sub, audit = build_leave_reclass(data, emp_sub, target_runs[0])
    has_477_6_from_detail = any(k[0] == '477.6' for k in gl_agg)
    if not has_477_6_from_detail:
        print(f"\n  [Adding Leave Loading Expense → 477.6 from Data export (Detail Report missed it)]")
        for k, v in additions.items():
            if k[0] == '477.6':
                gl_agg[k] = gl_agg.get(k, 0) + v
                print(f"    +${v:,.2f} → 477.6 {k[1]}")
    else:
        print(f"\n  [Detail Report has 477.6 — no Data-export reclass needed]")

    combined = dict(gl_agg)

    # 4. Print final journal lines (DR side)
    print(f"\n=== Final expense DR journal lines ===")
    by_gl = defaultdict(float)
    by_branch_gl = defaultdict(float)
    print(f"  {'GL':<8} {'Sub Account':<32} {'Branch':<6} {'Amount':>14}")
    rows_out = []
    total = 0.0
    for (gl, sub), v in sorted(combined.items()):
        if abs(v) < 0.005:
            continue
        b = sub_to_branch(sub)
        print(f"  {gl:<8} {sub:<32} {b:<6} ${v:>12,.2f}")
        by_gl[gl] += v
        by_branch_gl[(b, gl)] += v
        rows_out.append({'gl': gl, 'sub': sub, 'branch': b, 'amount': round(v, 2)})
        total += v
    print(f"  {'TOTAL':<48} ${total:>12,.2f}")

    print(f"\n=== Totals by GL ===")
    for gl, v in sorted(by_gl.items()):
        print(f"  {gl:<10} ${v:>12,.2f}")

    # Compare to Craig's known journal for PAY-001910
    if target_run == 'PAY-001910':
        print(f"\n=== Reconciliation vs Journal #673782 ===")
        CRAIG = {
            ('SC','477'): 133812.66, ('WB','477'): 22173.51,
            ('SC','477.4'): 46202.41, ('WB','477.4'): 1250.00,
            ('SC','477.6'): 815.93,
            ('SC','477.7'): 336.78,
            ('SC','478'): 15422.67, ('WB','478'): 2510.03,
            ('SC','478.1'): 5748.23, ('WB','478.1'): 150.00,
            ('SC','918'): 4662.41,
        }
        ok = True
        for (br, gl), expected in CRAIG.items():
            actual = by_branch_gl.get((br, gl), 0)
            delta = actual - expected
            flag = '✓' if abs(delta) < 0.50 else '✗'
            print(f"  {flag}  {br} {gl:<8} expected ${expected:>12,.2f}  actual ${actual:>12,.2f}  Δ ${delta:+9,.2f}")
            if abs(delta) >= 0.50:
                ok = False
        print(f"\n{'RECONCILED ✓' if ok else 'MISMATCH ✗ (review variance with Nicole before posting)'}")

    # 5. Build CR side (payables + 877 clearing) from Summary tuples
    print(f"\n=== Building CR side (payables + 877 clearing) ===")
    # SC tenant payables = SC branch + WB branch combined
    sc_net = by_branch.get('SC', {}).get('net', 0) + by_branch.get('WB', {}).get('net', 0)
    sc_pretax = by_branch.get('SC', {}).get('pretax_ded', 0) + by_branch.get('WB', {}).get('pretax_ded', 0)
    sc_posttax = by_branch.get('SC', {}).get('post_tax_ded', 0) + by_branch.get('WB', {}).get('post_tax_ded', 0)
    sc_payg = by_branch.get('SC', {}).get('payg', 0) + by_branch.get('WB', {}).get('payg', 0)
    sc_super = by_branch.get('SC', {}).get('employer_super', 0) + by_branch.get('WB', {}).get('employer_super', 0)

    cq_net = by_branch.get('CQ', {}).get('net', 0)
    cq_pretax = by_branch.get('CQ', {}).get('pretax_ded', 0)
    cq_posttax = by_branch.get('CQ', {}).get('post_tax_ded', 0)
    cq_payg = by_branch.get('CQ', {}).get('payg', 0)
    cq_super = by_branch.get('CQ', {}).get('employer_super', 0)

    sc_dr_total = sum(v for (gl, sub), v in combined.items()
                      if sub.startswith(('SC', 'WB')) or sub == '' or sub.startswith('??'))
    cq_dr_total = sum(v for (gl, sub), v in combined.items()
                      if sub.startswith('CQ'))

    print(f"\n  SC tenant (SC + WB branches combined):")
    print(f"    DR side total      : ${sc_dr_total:>12,.2f}")
    print(f"    803 Wages Payable  : ${(sc_net + sc_pretax + sc_posttax):>12,.2f}")
    print(f"    825 PAYG Payable   : ${sc_payg:>12,.2f}")
    print(f"    826 Super Payable  : ${sc_super:>12,.2f}")
    sc_cr_total = sc_net + sc_pretax + sc_posttax + sc_payg + sc_super
    print(f"    CR side total      : ${sc_cr_total:>12,.2f}")
    print(f"    Δ (must = 877 clearing): ${sc_dr_total - sc_cr_total:+,.2f}")
    print(f"    [877 Tracking Transfer CR (location-tagged) = ${sc_dr_total:,.2f}]")
    print(f"    [877 Tracking Transfer DR (untracked)        = ${sc_cr_total:,.2f}]")

    print(f"\n  CQ tenant (CQ only — no location tracking):")
    print(f"    DR side total      : ${cq_dr_total:>12,.2f}")
    print(f"    803 Wages Payable  : ${(cq_net + cq_pretax + cq_posttax):>12,.2f}")
    print(f"    825 PAYG Payable   : ${cq_payg:>12,.2f}")
    print(f"    826 Super Payable  : ${cq_super:>12,.2f}")
    cq_cr_total = cq_net + cq_pretax + cq_posttax + cq_payg + cq_super
    print(f"    CR side total      : ${cq_cr_total:>12,.2f}")
    print(f"    Δ DR-CR (should be 0): ${cq_dr_total - cq_cr_total:+,.2f}")

    # 6. Summary totals comparison
    print(f"\n=== Summary totals by branch (from Pay Activity Summary) ===")
    print(f"{'Branch':<8} {'gross':>12} {'pretax_ded':>12} {'payg':>10} {'after_tax':>10} {'post_tax_ded':>14} {'net':>12} {'employer_super':>14}")
    for branch, t in sorted(by_branch.items()):
        print(f"  {branch:<6} {t['gross']:>12,.2f} {t['pretax_ded']:>12,.2f} {t['payg']:>10,.2f} {t['after_tax']:>10,.2f} {t['post_tax_ded']:>14,.2f} {t['net']:>12,.2f} {t['employer_super']:>14,.2f}")


if __name__ == '__main__':
    main()
