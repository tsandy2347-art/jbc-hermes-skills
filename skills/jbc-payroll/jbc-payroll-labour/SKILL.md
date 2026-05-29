---
name: jbc-payroll-labour
description: JBC payroll & labour analyser specialist (read-only). Ingests MYOB Pay Activity Summary + AlayaCare timesheet CSV exports (no APIs) and runs ten detectors across six domains — pay-line variance (SCHADS recompute), unverified-line, systemic-underpayment, super-miscalc, ghost-shift, duplicate-payline, labour-cost-pct, utilisation-drop, overtime-spike, broken-shift-trigger. Writes findings + an audit_runs row to the shared JBC findings DB. Sibling to the existing `jbc-payroll/create-payroll-journal` write skill — this skill NEVER writes a journal.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, payroll, labour, schads, myob, alayacare, finance]
    category: jbc-finance
required_environment_variables:
  - name: JBC_FINDINGS_DATABASE_URL
    prompt: Postgres connection string for the shared JBC findings DB (falls back to HERMES_FINDINGS_DATABASE_URL if unset).
    required_for: writing findings + audit_runs
optional_environment_variables:
  - name: HERMES_FINDINGS_DATABASE_URL
    prompt: Fallback name for the findings DB connection string.
  - name: MYOB_EXPORT_PATH
    prompt: "Absolute path to the MYOB Pay Activity Summary CSV export. Default: /data/hermes/imports/myob_latest.csv."
  - name: ALAYACARE_EXPORT_PATH
    prompt: "Absolute path to the AlayaCare timesheet CSV export. Default: /data/hermes/imports/alayacare_latest.csv."
  - name: SCHADS_RULESET_PATH
    prompt: "Path to a JSON ruleset file containing SCHADS rates + superRate + effectiveFrom/effectiveTo. If missing the per-line SCHADS recompute is skipped (still emits unverified-line for every line)."
  - name: PAY_VARIANCE_TOLERANCE_AUD
    prompt: "Tolerance for per-line and super variance (AUD). Default 1.00."
  - name: SYSTEMIC_MIN_STAFF_AFFECTED
    prompt: "Minimum distinct staff with the same underpaid lineType to trigger systemic-underpayment. Default 3."
  - name: LABOUR_COST_TARGET_PCT_SC
    prompt: "Target labour-cost-as-%-of-revenue for SC. Default 70."
  - name: LABOUR_COST_TARGET_PCT_CQ
    prompt: "Target labour-cost-as-%-of-revenue for CQ. Default 70."
  - name: OVERTIME_SPEND_ALERT_PCT
    prompt: "OT spend alert threshold as % of total payroll. Default 5."
  - name: UTILISATION_FLOOR_PCT
    prompt: "Billable / paid hours utilisation floor. Default 85."
  - name: SC_REVENUE_AUD
    prompt: "Optional per-run SC revenue (AUD) for labour-cost-% — when set, used in place of a Xero pull (which is out of scope for this read-only CSV skill v0.1.0)."
  - name: CQ_REVENUE_AUD
    prompt: "Optional per-run CQ revenue (AUD) for labour-cost-%."
---

# jbc-payroll-labour — payroll/labour analyser (read-only)

Assumes `jbc-context` is in scope. Strictly READ-ONLY. The Craig-pattern
DRAFT manual journal is a SEPARATE sibling skill
(`jbc-payroll/create-payroll-journal`) — this skill never touches it.

## Source data

No APIs. The user (or an upstream ingest cron) drops two CSVs:

- `MYOB_EXPORT_PATH` (default `/data/hermes/imports/myob_latest.csv`) —
  parsed MYOB Pay Activity Summary. One row per (entity, employee,
  lineType, amount). Hourly detail (start/end timestamps) optional.
- `ALAYACARE_EXPORT_PATH` (default `/data/hermes/imports/alayacare_latest.csv`)
  — timesheets / roster. Required only for `ghost-shift`,
  `overtime-spike`, `utilisation-drop`, `broken-shift-trigger`.

If a required CSV is missing the run does NOT crash — it emits an
`ingest`-domain finding (`myob-export-missing` or
`alayacare-export-missing`) and dependent detectors skip.

### MYOB CSV expected columns

```
entity_code   SC | CQ
employee_id   string (MYOB employee id or name slug)
employee_name string
pay_run_id    string (pay-period identifier)
period_start  ISO date
period_end    ISO date
line_type     gross | pre-tax-ded | payg | post-tax-ded | ee-super |
              er-super | super | overtime | allowance | penalty-* | ...
amount        decimal AUD
classification optional (e.g. "Home Care Employee Level 2 PT")
employment_type optional (casual | permanent)
hours         optional decimal
```

### AlayaCare CSV expected columns

```
entity_code   SC | CQ
employee_id   matches MYOB employee_id
employee_name string
shift_id      string
start_ts      ISO datetime (AEST)
end_ts        ISO datetime
billable_hours decimal
paid_hours    decimal
shift_kind    roster | shift | travel | broken-gap | ...
```

## Detectors

| Code | Domain | Severity policy | is_people_flag |
|---|---|---|---|
| `pay-line-variance` | award | critical if underpaid ≥ $50, else warning | true |
| `unverified-line` | award | warning | true |
| `systemic-underpayment` | award | critical | false (pattern, not name) |
| `super-miscalc` | integrity | critical if missing or under ≥ $50, else warning | false (run-level) |
| `ghost-shift` | integrity | warning | true |
| `duplicate-payline` | integrity | warning | true |
| `labour-cost-pct` | labour-cost | warning above target +5pp, else info | false |
| `utilisation-drop` | utilisation | warning | false |
| `overtime-spike` | overtime | warning | false |
| `broken-shift-trigger` | rostering | info | false |

## Hard rules

1. READ-ONLY. No writes to MYOB, AlayaCare, Xero, or any external system.
2. `source_agent = 'payroll-labour'` for every row.
3. `is_people_flag` set per the table above. Per-employee findings → true.
4. Dedup via `evidence.dedupKey`. Convention:
   - `pay-line-variance:<entity>:<employeeId>:<payRunId>:<lineType>`
   - `unverified-line:<entity>:<employeeId>:<payRunId>:<lineType>`
   - `systemic-underpayment:<entity>:<lineType>:<payRunId>`
   - `super-miscalc:<entity>:<payRunId>`
   - `ghost-shift:<entity>:<employeeId>:<payRunId>`
   - `duplicate-payline:<entity>:<employeeId>:<lineType>:<amount>:<payRunId>`
   - `labour-cost-pct:<entity>:<period>`
   - `utilisation-drop:<entity>:<period>`
   - `overtime-spike:<entity>:<payRunId>`
   - `broken-shift-trigger:<entity>:<employeeId>:<shiftId>`
   - `myob-export-missing:<isoDate>` / `alayacare-export-missing:<isoDate>`

## Invocation

```
python3 /data/hermes/skills/jbc-payroll-labour/scripts/run_payroll_labour.py
```

## Files

```
jbc-payroll-labour/
  SKILL.md
  scripts/
    run_payroll_labour.py         # orchestrator + audit_runs row mgmt
    myob_csv.py                   # MYOB CSV reader (graceful missing)
    alayacare_csv.py              # AlayaCare CSV reader (graceful missing)
    schads_engine.py              # deterministic recompute (best-effort)
    detectors/
      __init__.py
      award.py                    # pay-line-variance, unverified-line, systemic-underpayment
      integrity.py                # super-miscalc, ghost-shift, duplicate-payline
      labour.py                   # labour-cost-pct, utilisation-drop, overtime-spike
      rostering.py                # broken-shift-trigger
```

## Deliberately not ported (TODO)

- Email + SMS delivery (Mark + brief-builder cover this).
- Anthropic `classifyException` (`ai_explanation` left NULL).
- The writeable Excel `Payroll Validation` workbook.
- Live Xero revenue pull for labour-cost-% — surfaced as
  `SC_REVENUE_AUD` / `CQ_REVENUE_AUD` env override for now.
- Mirus parity test harness (Phase 7).
