"""parse_myob_payroll.py — MYOB → Xero DRAFT Manual Journal generator v0.5.0

Reads three MYOB exports and produces Craig-pattern DRAFT journals for SC and CQ tenants.

INPUTS:
  1. Pay Activity Summary .xlsx          (gross/PAYG/super tuples — per-run × branch × dept)
  2. Pay Activity Detail Data .xlsx       (flat tabular — for Leave Loading reclass)
  3. Pay Activity Detail Report .xlsx     (hierarchical w/ GL+SubAccount stamps — source of truth)

OUTPUTS:
  PARAMS dict ready for Xero POST:
    - SC tenant journal lines (location-tagged DRs + payable CRs + 877 summary)
    - CQ tenant journal lines (DRs + CRs, no location)

KEY RULES (calibrated against Craig's J#673782 / PAY-001910):
  - MYOB stamps GL on each pay line — use Pay Activity Detail Report as source of truth
  - Annual Leave Taken → 918, Personal Leave Taken → 477.7 (already stamped in Detail Report)
  - Leave Loading Expense → 477.6 (only in Data export — must add)
  - Travel allowances + sleepover super → no GL stamp → allocate to 477/478 by employee's primary sub-account
  - PAY-001911/1912 (adhoc SC corrections) combined into PAY-001910 main journal
  - 877 Tracking Transfers: one CR per location (sums DR for that location), one untracked DR (sums CR payables)

USAGE:
    python3 parse_myob_payroll.py <summary.xlsx> <data.xlsx> <detail_report.xlsx>
        [--sc-runs PAY-001910,PAY-001911 --cq-runs PAY-001909]
        [--journal-date 2026-04-22 --post-draft]

    Without --post-draft, just renders the proposal.
    With --post-draft, requires XERO_SC_* + XERO_CQ_* env vars.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
SMNS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'

# ── MYOB sub-account prefix → Xero Location/Branch ─────────────────────────────
SUB_BRANCH = {'SC': 'SC', 'WB': 'WB', 'CQ': 'CQ'}

# Pay runs grouped by tenant (auto-detected from sub-account prefix per run)
SC_TENANT_BRANCHES = ('SC', 'WB')
CQ_TENANT_BRANCHES = ('CQ',)

# MYOB Detail Report sections we POST to Xero (Entitlement Accrual is info-only — skip)
SECTIONS = ('Gross Income', 'Tax Free Income', 'Pre-tax Deductions',
            'Employer Superannuation', 'Entitlement Accrual',
            'Deductions', 'Net Pay', 'After-tax Income')
POSTING_SECTIONS = ('Gross Income', 'Tax Free Income', 'Employer Superannuation')

# Patterns for scan-by-content (Detail Report layout shifts per section)
SUB_PATTERN = re.compile(r'^[A-Z]{2}\d{2}-[A-Z]{2}-')
GL_PATTERN = re.compile(r'^(\d{3,4}(?:\.\d+)?)\s*-\s*(.+)')
CONT_WORDS = ('Direct', 'Indirect', 'Salaries', 'Leave', 'Clearing', 'Wages')

# Leave-payment items not stamped in Detail Report
LEAVE_PAYMENT_RULES = {
    'Leave Loading Expense': {'code': '477.6', 'name': 'Vacation Leave'},
}

# Xero account codes
CODES = {
    'wages_direct':   '477',
    'wages_indirect': '477.4',
    'vac_leave':      '477.6',
    'sick_leave':     '477.7',
    'super_direct':   '478',
    'super_indirect': '478.1',
    'prov_al':        '918',
    'wages_payable':  '803',
    'payg_payable':   '825',
    'super_payable':  '826',
    'tracking_xfer':  '877',
}

# Xero API
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_API = "https://api.xero.com/api.xro/2.0"
WRITE_SCOPES = "accounting.transactions accounting.settings.read"


# ── Generic xlsx readers ──────────────────────────────────────────────────────

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
    hdr = {rows[1].get(i): i for i in rows[1]}
    return [{k: rows[r].get(i) for k, i in hdr.items()}
            for r in range(2, max(rows) + 1)]


def _num(v):
    if v is None or v == '': return 0.0
    try: return float(v)
    except (TypeError, ValueError): return 0.0


def _excel_to_iso(v):
    if v is None or v == '': return ''
    s = str(v).strip()
    if re.match(r'\d{4}-\d{1,2}-\d{1,2}', s): return s
    if re.match(r'\d{1,2}/\d{1,2}/\d{4}', s):
        d, m, y = s.split('/')
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    try:
        serial = int(float(s))
    except (ValueError, TypeError):
        return s
    import datetime as _dt
    return (_dt.date(1899, 12, 30) + _dt.timedelta(days=serial)).isoformat()


# ── Pattern scanners ──────────────────────────────────────────────────────────

def _find_sub(row):
    for v in row.values():
        if v and isinstance(v, str) and SUB_PATTERN.match(v.strip()):
            return v.strip()
    return ''


def _find_gl(row):
    for v in row.values():
        if v and isinstance(v, str):
            m = GL_PATTERN.match(v.strip())
            if m: return v.strip(), m.group(1)
    return '', ''


# ── 1. Pay Activity Summary → per-(pay_run, branch) totals ────────────────────

def parse_summary(path):
    """Walk Summary hierarchy. Returns {(pay_run, branch): {field: amount}}.

    Hierarchy:
      Row col 1: 'PAY-NNNN (completed)' OR header metadata
      Row col 2: Branch name
      Row col 3: Department name
      Row col 4-: empty (lower levels)
    """
    rows = _load_rows(path)
    by_run_branch = defaultdict(lambda: {
        'gross': 0.0, 'pretax_ded': 0.0, 'payg': 0.0, 'after_tax': 0.0,
        'post_tax_ded': 0.0, 'net': 0.0, 'employer_super': 0.0,
    })
    meta = {}
    current_run = None
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
            m = re.search(r'PAY-\d+', str(a))
            if m:
                # Skip cancelled / voided runs — they shouldn't post to Xero
                status_l = str(a).lower()
                if any(bad in status_l for bad in ('cancelled', 'voided', 'deleted')):
                    current_run = None  # skip subsequent dept rows for this run
                else:
                    current_run = m.group(0)
            current_branch = None
            continue
        if b:
            current_branch = BRANCH_NORM.get(re.sub(r'\s+', ' ', str(b).strip()).lower())
            continue
        if c and current_branch and current_run:
            key = (current_run, current_branch)
            by_run_branch[key]['gross']          += _num(row.get(6))
            by_run_branch[key]['pretax_ded']     += _num(row.get(7))
            by_run_branch[key]['payg']           += _num(row.get(9))
            by_run_branch[key]['after_tax']      += _num(row.get(10))
            by_run_branch[key]['post_tax_ded']   += _num(row.get(11))
            by_run_branch[key]['net']            += _num(row.get(12))
            by_run_branch[key]['employer_super'] += _num(row.get(14))

    for k in by_run_branch:
        for f in by_run_branch[k]:
            by_run_branch[k][f] = round(by_run_branch[k][f], 2)
    return dict(by_run_branch), meta


# ── 2. Data export → leave-loading expense lines (by employee→sub) ────────────

def parse_data(path):
    return _load_tabular(path)


# ── 3. Detail Report → GL-stamped expense aggregation ─────────────────────────

def aggregate_detail(detail_path, target_runs):
    """Walk Pay Activity Detail Report, return:
      gl_agg: dict[(gl_code, sub)] → amount (only POSTING_SECTIONS, only target_runs)
      emp_sub: dict[emp_id] → sub-account (super-row preferred)
    """
    if isinstance(target_runs, str):
        target_runs = (target_runs,)
    rows = _load_rows(detail_path)
    gl_agg = defaultdict(float)

    # Pass 1: build emp_sub
    emp_sub_super = {}
    emp_sub_first = {}
    current_section = None
    current_emp = None
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
            sub = _find_sub(row) or _find_sub(rows.get(r + 1, {}))
            if sub and current_emp:
                if current_section == 'Employer Superannuation':
                    emp_sub_super[current_emp] = sub
                elif current_emp not in emp_sub_first:
                    emp_sub_first[current_emp] = sub
    emp_sub = {**emp_sub_first, **emp_sub_super}

    # Pass 2: aggregate GL
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
            gl_str, gl_code = _find_gl(row)
            if not gl_str:
                gl_str, gl_code = _find_gl(rows.get(r + 1, {}))
            sub = _find_sub(row) or _find_sub(rows.get(r + 1, {}))
            val = _num(row.get(25)) or current_item_total
            if val == 0:
                continue
            if not gl_code:
                # Orphan — allocate to 477 (income) or 478 (super) by emp primary sub
                sub_for_orphan = sub or emp_sub.get(current_emp or '', '')
                if not sub_for_orphan:
                    continue  # truly unallocatable
                target_gl = '478' if current_section == 'Employer Superannuation' else '477'
                gl_agg[(target_gl, sub_for_orphan)] += val
            else:
                if not sub:
                    # GL stamped but no sub — fall back to emp's primary sub
                    sub = emp_sub.get(current_emp or '', '')
                gl_agg[(gl_code, sub)] += val

    return dict(gl_agg), emp_sub


def add_leave_loading(gl_agg, data, emp_sub, target_runs):
    """Leave Loading Expense isn't stamped in Detail Report — add from Data export."""
    if isinstance(target_runs, str):
        target_runs = (target_runs,)
    if any(k[0] == '477.6' for k in gl_agg):
        return gl_agg  # already covered
    for d in data:
        if d.get('Pay Run ID') not in target_runs:
            continue
        if d.get('Pay Item Description') != 'Leave Loading Expense':
            continue
        amt = _num(d.get('Amount'))
        if amt == 0:
            continue
        emp_id = str(d.get('Employee ID') or '')
        sub = emp_sub.get(emp_id, '')
        if not sub:
            continue
        gl_agg[('477.6', sub)] = gl_agg.get(('477.6', sub), 0) + amt
    return gl_agg


# ── 4. Group GL aggregation by tenant ─────────────────────────────────────────

def split_by_tenant(gl_agg):
    """Group {(gl, sub): amount} into SC tenant and CQ tenant buckets."""
    sc = defaultdict(float)
    cq = defaultdict(float)
    unknown = defaultdict(float)
    for (gl, sub), v in gl_agg.items():
        prefix = (sub[:2] if sub else '').upper()
        if prefix in SC_TENANT_BRANCHES:
            sc[(gl, sub)] += v
        elif prefix in CQ_TENANT_BRANCHES:
            cq[(gl, sub)] += v
        else:
            unknown[(gl, sub)] += v
    return dict(sc), dict(cq), dict(unknown)


# ── 5. Build full balanced journals ───────────────────────────────────────────

def build_sc_journal(sc_dr_lines, summary_by_run_branch, sc_runs):
    """Build SC tenant journal: DRs + payable CRs.

    Craig's actual pattern (verified via API on Journal #673782 = ID a747fe21):
    - 23 expense DR lines, each Location-tagged
    - 22 payable CR lines (803/825/826), UNTRACKED, grouped by some implicit
      posting class (Craig had multiple per account — we collapse to one per
      tenant for simplicity)
    - NO 877 entries (the 877 visible in print/Account Transactions is a
      Xero-side auto-display for tracking-imbalance — not in the journal)

    DR == CR by gross-pay identity: Gross+Super = Net + PAYG + Super + PreTax + PostTax
    Any small variance from Detail/Summary mismatch absorbed into largest 477 line.
    """
    expense_dr_lines = []
    dr_sum = 0.0

    # Build expense DR lines + track by-location totals (for bal-adj on 477)
    for (gl, sub), amt in sorted(sc_dr_lines.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if amt == 0:
            continue
        loc = 'Sunshine Coast' if sub.startswith('SC') else ('Wide Bay' if sub.startswith('WB') else None)
        line = {
            'LineAmount': round(amt, 2),
            'AccountCode': gl,
            'Description': f"{sub} ({gl})",
            'Tracking': [],
        }
        if loc:
            line['_location_name'] = loc
        expense_dr_lines.append(line)
        dr_sum += round(amt, 2)

    # Payable CRs (untracked) from Summary tuples — SC + WB combined
    sc_net = sum(summary_by_run_branch.get((run, br), {}).get('net', 0)
                 for run in sc_runs for br in ('SC', 'WB'))
    sc_pretax = sum(summary_by_run_branch.get((run, br), {}).get('pretax_ded', 0)
                    for run in sc_runs for br in ('SC', 'WB'))
    sc_posttax = sum(summary_by_run_branch.get((run, br), {}).get('post_tax_ded', 0)
                     for run in sc_runs for br in ('SC', 'WB'))
    sc_payg = sum(summary_by_run_branch.get((run, br), {}).get('payg', 0)
                  for run in sc_runs for br in ('SC', 'WB'))
    sc_super = sum(summary_by_run_branch.get((run, br), {}).get('employer_super', 0)
                   for run in sc_runs for br in ('SC', 'WB'))

    wages_pay = round(sc_net + sc_pretax + sc_posttax, 2)
    payg_pay = round(sc_payg, 2)
    super_pay = round(sc_super, 2)
    cr_sum = wages_pay + payg_pay + super_pay

    # Balance adjustment — absorb DR-CR variance on largest SC 477 line
    delta = round(cr_sum - dr_sum, 2)
    if abs(delta) >= 0.01:
        target_idx = None
        target_amt = 0.0
        for i, line in enumerate(expense_dr_lines):
            if line['AccountCode'] == '477' and line.get('_location_name') == 'Sunshine Coast':
                if line['LineAmount'] > target_amt:
                    target_amt = line['LineAmount']
                    target_idx = i
        if target_idx is not None:
            expense_dr_lines[target_idx]['LineAmount'] = round(
                expense_dr_lines[target_idx]['LineAmount'] + delta, 2)
            expense_dr_lines[target_idx]['Description'] += f" [+${delta:,.2f} bal-adj]"

    payable_cr_lines = [
        {'LineAmount': -wages_pay, 'AccountCode': CODES['wages_payable'],
         'Description': 'Net pay + salary-sacrifice + post-tax deductions (SC + WB)', 'Tracking': []},
        {'LineAmount': -payg_pay, 'AccountCode': CODES['payg_payable'],
         'Description': 'PAYG withholdings (SC + WB)', 'Tracking': []},
        {'LineAmount': -super_pay, 'AccountCode': CODES['super_payable'],
         'Description': 'Employer super SG (SC + WB)', 'Tracking': []},
    ]

    return expense_dr_lines + payable_cr_lines


def build_cq_journal(cq_dr_lines, summary_by_run_branch, cq_runs):
    """Build CQ tenant journal: DRs + payable CRs (no location, no 877).

    Forces DR == CR by adjusting the 477 wages-direct DR to absorb any
    Detail Report vs Summary tuple variance (typically small orphan lines).
    """
    lines = []
    dr_sum = 0.0
    for (gl, sub), amt in sorted(cq_dr_lines.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if amt == 0:
            continue
        lines.append({
            'LineAmount': round(amt, 2),
            'AccountCode': gl,
            'Description': f"{sub} ({gl})" if sub else gl,
            'Tracking': [],
        })
        dr_sum += round(amt, 2)

    cq_net = sum(summary_by_run_branch.get((run, 'CQ'), {}).get('net', 0) for run in cq_runs)
    cq_pretax = sum(summary_by_run_branch.get((run, 'CQ'), {}).get('pretax_ded', 0) for run in cq_runs)
    cq_posttax = sum(summary_by_run_branch.get((run, 'CQ'), {}).get('post_tax_ded', 0) for run in cq_runs)
    cq_payg = sum(summary_by_run_branch.get((run, 'CQ'), {}).get('payg', 0) for run in cq_runs)
    cq_super = sum(summary_by_run_branch.get((run, 'CQ'), {}).get('employer_super', 0) for run in cq_runs)

    wages_pay = round(cq_net + cq_pretax + cq_posttax, 2)
    payg_pay = round(cq_payg, 2)
    super_pay = round(cq_super, 2)
    cr_sum = wages_pay + payg_pay + super_pay

    # Balance adjustment — absorb DR-CR variance on 477 (catch-all wages account)
    delta = round(cr_sum - dr_sum, 2)
    if abs(delta) >= 0.01:
        # Find the largest 477 line and adjust it (or add a new line)
        adjusted = False
        for line in lines:
            if line['AccountCode'] == '477':
                line['LineAmount'] = round(line['LineAmount'] + delta, 2)
                line['Description'] += f" [+${delta:,.2f} bal-adj]"
                adjusted = True
                break
        if not adjusted:
            # No 477 line existed — add one
            lines.append({
                'LineAmount': delta,
                'AccountCode': '477',
                'Description': f'Balance adjustment to match Summary tuple totals',
                'Tracking': [],
            })

    lines.append({'LineAmount': -wages_pay, 'AccountCode': CODES['wages_payable'],
                  'Description': 'Net pay + deductions (CQ)', 'Tracking': []})
    lines.append({'LineAmount': -payg_pay, 'AccountCode': CODES['payg_payable'],
                  'Description': 'PAYG withholdings (CQ)', 'Tracking': []})
    lines.append({'LineAmount': -super_pay, 'AccountCode': CODES['super_payable'],
                  'Description': 'Employer super SG (CQ)', 'Tracking': []})
    return lines


def balance_check(lines, label):
    dr = sum(l['LineAmount'] for l in lines if l['LineAmount'] > 0)
    cr = -sum(l['LineAmount'] for l in lines if l['LineAmount'] < 0)
    bal = abs(dr - cr) < 0.01
    return dr, cr, bal


def render_journal(lines, title):
    dr, cr, bal = balance_check(lines, title)
    print(f"\n{'═' * 80}")
    print(f"{title}")
    print(f"{'═' * 80}")
    print(f"{'Acct':<8} {'Description':<55} {'Loc':<14} {'Amount':>14}")
    for l in lines:
        loc = l.get('_location_name', '')[:14]
        amt = l['LineAmount']
        sign = f"DR ${amt:>11,.2f}" if amt > 0 else f"CR ${-amt:>11,.2f}"
        print(f"  {l['AccountCode']:<6} {l['Description'][:55]:<55} {loc:<14} {sign}")
    print(f"  {'─' * 78}")
    print(f"  TOTAL DR: ${dr:>12,.2f}    TOTAL CR: ${cr:>12,.2f}    Balanced: {'✓' if bal else '✗'}")
    return dr, cr, bal


# ── 6. Xero POST integration ──────────────────────────────────────────────────

def _creds(entity):
    p = entity.upper()
    return {
        'client_id':     os.environ.get(f'XERO_{p}_CLIENT_ID', ''),
        'client_secret': os.environ.get(f'XERO_{p}_CLIENT_SECRET', ''),
        'tenant_id':     os.environ.get(f'XERO_{p}_TENANT_ID', ''),
    }


def _xero_token(creds):
    import base64, urllib.request, urllib.error
    if not creds['client_id'] or not creds['client_secret']:
        raise RuntimeError(f"XERO_*_CLIENT_ID / _CLIENT_SECRET env vars not set")
    basic = base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    req = urllib.request.Request(
        XERO_TOKEN_URL,
        data=f"grant_type=client_credentials&scope={WRITE_SCOPES}".encode(),
        headers={'Authorization': f'Basic {basic}',
                 'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())['access_token']
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Xero token exchange failed: {e.code} {e.read().decode()[:300]}") from e


def _xero_get(creds, token, path):
    import urllib.request, urllib.error
    req = urllib.request.Request(
        f"{XERO_API}{path}",
        headers={'Authorization': f'Bearer {token}',
                 'Xero-Tenant-Id': creds['tenant_id'],
                 'Accept': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Xero GET {path} failed: {e.code} {e.read().decode()[:300]}") from e


def discover_sc_tracking(creds, token):
    """Find SC Location tracking category + SC/WB option IDs + system account IDs."""
    data = _xero_get(creds, token, "/TrackingCategories")
    cats = data.get('TrackingCategories', [])
    target = next((c for c in cats if c.get('Name', '').strip().lower() == 'location'
                   and c.get('Status') == 'ACTIVE'), None)
    if not target:
        raise RuntimeError(f"No ACTIVE 'Location' tracking on SC — saw: {[c.get('Name') for c in cats]}")
    options = {o.get('Name', '').strip(): o for o in target.get('Options', []) if o.get('Status') == 'ACTIVE'}
    sc = options.get('Sunshine Coast')
    wb = options.get('Wide Bay')
    if not sc or not wb:
        raise RuntimeError(f"Sunshine Coast/Wide Bay not in Location options — saw: {list(options.keys())}")
    # Look up 877 AccountID (it's a system account — must use AccountID not AccountCode)
    accounts_data = _xero_get(creds, token, "/Accounts")
    acct_877 = next((a for a in accounts_data.get('Accounts', [])
                     if a.get('Code') == '877'), None)
    if not acct_877:
        raise RuntimeError("Account 877 not found in SC chart")
    return {
        'category_id': target['TrackingCategoryID'],
        'sc_option_id': sc['TrackingOptionID'],
        'wb_option_id': wb['TrackingOptionID'],
        'account_877_id': acct_877['AccountID'],
    }


def discover_cq_accounts(creds, token):
    """CQ — just need 877 AccountID (no tracking)."""
    accounts_data = _xero_get(creds, token, "/Accounts")
    acct_877 = next((a for a in accounts_data.get('Accounts', [])
                     if a.get('Code') == '877'), None)
    if not acct_877:
        raise RuntimeError("Account 877 not found in CQ chart")
    return {'account_877_id': acct_877['AccountID']}


def attach_tracking(lines, tracking):
    """Replace `_location_name` placeholders with Xero TrackingCategoryID/OptionID.
    Also swap AccountCode='877' for AccountID (877 is a Xero SYSTEM account —
    'Account code 877 has been removed as it does not match a recognised account'
    if posted by Code).
    """
    out = []
    for l in lines:
        l = dict(l)
        loc = l.pop('_location_name', None)
        if loc == 'Sunshine Coast':
            l['Tracking'] = [{'TrackingCategoryID': tracking['category_id'],
                              'TrackingOptionID':   tracking['sc_option_id']}]
        elif loc == 'Wide Bay':
            l['Tracking'] = [{'TrackingCategoryID': tracking['category_id'],
                              'TrackingOptionID':   tracking['wb_option_id']}]
        else:
            l['Tracking'] = []
        # Swap 877 code for AccountID (system account workaround)
        if l.get('AccountCode') == '877' and 'account_877_id' in tracking:
            l.pop('AccountCode', None)
            l['AccountID'] = tracking['account_877_id']
        out.append(l)
    return out


def post_draft(entity, narration, journal_date, lines):
    """POST a DRAFT Manual Journal to Xero. HARD LOCKED to DRAFT status."""
    import datetime as _dt
    import urllib.request, urllib.error
    creds = _creds(entity)
    if not creds['tenant_id']:
        raise RuntimeError(f"XERO_{entity}_TENANT_ID not set")
    token = _xero_token(creds)

    # SC needs location tracking + 877 system-account ID swap
    if entity.upper() == 'SC':
        tracking = discover_sc_tracking(creds, token)
        lines = attach_tracking(lines, tracking)
    else:
        # CQ — no location tracking, but still need 877 system-account ID swap (if used)
        cq_meta = discover_cq_accounts(creds, token)
        lines = [{k: v for k, v in l.items() if not k.startswith('_')} for l in lines]
        # Swap 877 code for AccountID if any line uses it
        for l in lines:
            if l.get('AccountCode') == '877':
                l.pop('AccountCode', None)
                l['AccountID'] = cq_meta['account_877_id']

    dr, cr, bal = balance_check(lines, entity)
    if not bal:
        raise RuntimeError(f"{entity} unbalanced: DR ${dr:.2f} CR ${cr:.2f}")

    tag = " [posted by Payroll Agent]"
    body = {
        'Date': journal_date,
        'Status': 'DRAFT',  # HARD LOCKED
        'LineAmountTypes': 'NoTax',
        'Narration': (narration + tag)[:2500],
        'JournalLines': lines,
    }
    req = urllib.request.Request(
        f"{XERO_API}/ManualJournals",
        data=json.dumps({'ManualJournals': [body]}).encode(),
        headers={'Authorization': f'Bearer {token}',
                 'Xero-Tenant-Id': creds['tenant_id'],
                 'Content-Type': 'application/json',
                 'Accept': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:600]
        raise RuntimeError(f"Xero {e.code}: {err}") from e

    mj = data['ManualJournals'][0]
    return {
        'tenant': entity,
        'ManualJournalID': mj['ManualJournalID'],
        'Status': mj.get('Status'),
        'TotalDR': round(dr, 2),
        'TotalCR': round(cr, 2),
        'LineCount': len(lines),
        'xero_link': f"https://go.xero.com/Bank/ViewManualJournal.aspx?ManualJournalID={mj['ManualJournalID']}",
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('summary', help='Pay Activity Summary .xlsx')
    ap.add_argument('data', help='Pay Activity Detail Data .xlsx (1000-row cap MUST be declined)')
    ap.add_argument('detail', help='Pay Activity Detail Report .xlsx')
    ap.add_argument('--sc-runs', help='Comma-separated SC tenant pay runs (e.g. PAY-001910,PAY-001911)')
    ap.add_argument('--cq-runs', help='Comma-separated CQ tenant pay runs (e.g. PAY-001909)')
    ap.add_argument('--journal-date', help='Journal date YYYY-MM-DD (default: pay period end)')
    ap.add_argument('--narration', help='Narration (default: auto-generated)')
    ap.add_argument('--post-draft', action='store_true', help='Actually POST to Xero (otherwise just preview)')
    ap.add_argument('--json', action='store_true', help='Emit machine-readable JSON to stdout (for UI integration)')
    args = ap.parse_args()

    if args.json:
        # Silence text output for JSON consumers
        import io
        sys.stdout = io.StringIO()  # capture all print() calls so they don't pollute JSON output
        _json_buffer = sys.stdout

    print(f"=== Loading MYOB exports ===")
    summary, meta = parse_summary(args.summary)
    print(f"  Pay Activity Summary: {len(summary)} (pay_run, branch) buckets")
    if meta:
        print(f"  Physical pay-date range: {_excel_to_iso(meta.get('from',''))} → {_excel_to_iso(meta.get('to',''))}")
    data = parse_data(args.data)
    print(f"  Pay Activity Detail Data: {len(data)} rows")

    # Auto-detect pay runs by tenant if not specified
    sc_runs_seen = set()
    cq_runs_seen = set()
    for (run, br), _ in summary.items():
        if br in SC_TENANT_BRANCHES:
            sc_runs_seen.add(run)
        elif br in CQ_TENANT_BRANCHES:
            cq_runs_seen.add(run)

    sc_runs = tuple(args.sc_runs.split(',')) if args.sc_runs else tuple(sorted(sc_runs_seen))
    cq_runs = tuple(args.cq_runs.split(',')) if args.cq_runs else tuple(sorted(cq_runs_seen))
    print(f"  SC tenant runs: {', '.join(sc_runs) or '(none)'}")
    print(f"  CQ tenant runs: {', '.join(cq_runs) or '(none)'}")

    # Aggregate per tenant
    sc_gl_agg = {}
    cq_gl_agg = {}
    sc_emp_sub = {}
    if sc_runs:
        sc_gl_agg, sc_emp_sub = aggregate_detail(args.detail, sc_runs)
        sc_gl_agg = add_leave_loading(sc_gl_agg, data, sc_emp_sub, sc_runs)
    if cq_runs:
        cq_gl_agg, cq_emp_sub = aggregate_detail(args.detail, cq_runs)
        cq_gl_agg = add_leave_loading(cq_gl_agg, data, cq_emp_sub, cq_runs)

    # Drop any cross-tenant noise (CQ subs in SC dict, SC subs in CQ dict)
    sc_gl_agg = {(gl, sub): v for (gl, sub), v in sc_gl_agg.items()
                 if not sub or sub[:2].upper() in SC_TENANT_BRANCHES}
    cq_gl_agg = {(gl, sub): v for (gl, sub), v in cq_gl_agg.items()
                 if not sub or sub[:2].upper() in CQ_TENANT_BRANCHES}

    # Build journals
    sc_lines = build_sc_journal(sc_gl_agg, summary, sc_runs) if sc_runs else []
    cq_lines = build_cq_journal(cq_gl_agg, summary, cq_runs) if cq_runs else []

    # Render
    pay_date = _excel_to_iso(meta.get('to', '')) or args.journal_date or ''
    journal_date = args.journal_date or pay_date
    narration = args.narration or f"Payroll pay run(s) {','.join(sc_runs)} — wk ending {pay_date}"

    if sc_lines:
        render_journal(sc_lines, f"SC TENANT JOURNAL — runs {','.join(sc_runs)} — date {journal_date}")
    if cq_lines:
        render_journal(cq_lines, f"CQ TENANT JOURNAL — runs {','.join(cq_runs)} — date {journal_date}")

    posted = {'sc': None, 'cq': None}
    if args.post_draft:
        print(f"\n=== Posting DRAFTs to Xero ===")
        if sc_lines:
            try:
                result = post_draft('SC', narration + ' (SC + Wide Bay)', journal_date, sc_lines)
                posted['sc'] = result
                print(f"  SC: ✓ ManualJournalID={result['ManualJournalID']} ({result['Status']}) "
                      f"DR=${result['TotalDR']:,.2f} CR=${result['TotalCR']:,.2f}")
                print(f"      Link: {result['xero_link']}")
            except Exception as e:
                posted['sc'] = {'error': str(e)}
                print(f"  SC: ✗ {e}")
        if cq_lines:
            try:
                cq_narration = narration.replace('SC + Wide Bay', '').strip() + ' (CQ)'
                result = post_draft('CQ', cq_narration, journal_date, cq_lines)
                posted['cq'] = result
                print(f"  CQ: ✓ ManualJournalID={result['ManualJournalID']} ({result['Status']}) "
                      f"DR=${result['TotalDR']:,.2f} CR=${result['TotalCR']:,.2f}")
                print(f"      Link: {result['xero_link']}")
            except Exception as e:
                posted['cq'] = {'error': str(e)}
                print(f"  CQ: ✗ {e}")
    else:
        print(f"\n[Preview mode — pass --post-draft to actually POST to Xero]")

    if args.json:
        # Compute PAYG totals from Summary tuples
        sc_payg = sum(summary.get((run, br), {}).get('payg', 0) for run in sc_runs for br in ('SC','WB'))
        sc_super = sum(summary.get((run, br), {}).get('employer_super', 0) for run in sc_runs for br in ('SC','WB'))
        sc_net = sum(summary.get((run, br), {}).get('net', 0) for run in sc_runs for br in ('SC','WB'))
        cq_payg = sum(summary.get((run, br), {}).get('payg', 0) for run in cq_runs for br in ('CQ',))
        cq_super = sum(summary.get((run, br), {}).get('employer_super', 0) for run in cq_runs for br in ('CQ',))
        cq_net = sum(summary.get((run, br), {}).get('net', 0) for run in cq_runs for br in ('CQ',))

        # Sanitize lines for JSON
        def clean_lines(lines):
            return [{k: v for k, v in l.items() if not k.startswith('_')} for l in lines]

        result = {
            'ok': True,
            'meta': {
                'pay_period_from': _excel_to_iso(meta.get('from','')),
                'pay_period_to':   _excel_to_iso(meta.get('to','')),
                'journal_date':    journal_date,
                'sc_runs':         list(sc_runs),
                'cq_runs':         list(cq_runs),
            },
            'sc': {
                'lines':       clean_lines(sc_lines),
                'total_dr':    round(sum(l['LineAmount'] for l in sc_lines if l['LineAmount']>0), 2),
                'total_cr':    round(-sum(l['LineAmount'] for l in sc_lines if l['LineAmount']<0), 2),
                'payg':        round(sc_payg, 2),
                'super_sg':    round(sc_super, 2),
                'net_pay':     round(sc_net, 2),
                'narration':   narration + ' (SC + Wide Bay)' if sc_runs else None,
            } if sc_runs else None,
            'cq': {
                'lines':       clean_lines(cq_lines),
                'total_dr':    round(sum(l['LineAmount'] for l in cq_lines if l['LineAmount']>0), 2),
                'total_cr':    round(-sum(l['LineAmount'] for l in cq_lines if l['LineAmount']<0), 2),
                'payg':        round(cq_payg, 2),
                'super_sg':    round(cq_super, 2),
                'net_pay':     round(cq_net, 2),
                'narration':   narration.replace('SC + Wide Bay','').strip() + ' (CQ)' if cq_runs else None,
            } if cq_runs else None,
            'totals': {
                'payg_combined': round(sc_payg + cq_payg, 2),
                'super_combined': round(sc_super + cq_super, 2),
                'net_combined': round(sc_net + cq_net, 2),
            },
            'posted': posted,
            'log': _json_buffer.getvalue(),
        }
        # Restore stdout + emit clean JSON
        sys.stdout = sys.__stdout__
        print(json.dumps(result, default=str))


if __name__ == '__main__':
    main()
