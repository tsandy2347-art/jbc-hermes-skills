---
name: jbc-tax-compliance
description: Daily JBC tax-compliance specialist. READ-ONLY against Xero (SC + CQ tenants). Per-entity isolation — never consolidates SC + CQ for any statutory output. Runs detectors across GST (live position, coding anomalies, cash-set-aside shortfall), BAS (deadline), PAYG-withholding clearing position, super-clearing position, and QLD payroll-tax threshold. Writes findings + an audit_run row to the shared JBC findings DB. Replaces the legacy `tax-compliance-agent` Next.js Railway service. NEVER lodges with the ATO or QRO — humans lodge.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, tax, compliance, finance, xero, gst, bas, payg, super, payroll-tax]
    category: jbc-finance
required_environment_variables:
  - name: JBC_FINDINGS_DATABASE_URL
    prompt: Postgres connection string for the shared JBC findings DB (falls back to HERMES_FINDINGS_DATABASE_URL if unset)
    required_for: writing findings + audit_runs
  - name: XERO_SC_CLIENT_ID
    prompt: Xero SC tenant client ID
    required_for: SC detectors
  - name: XERO_SC_CLIENT_SECRET
    prompt: Xero SC tenant client secret
    required_for: SC detectors
  - name: XERO_SC_TENANT_ID
    prompt: Xero SC tenant UUID
    required_for: SC detectors
  - name: XERO_CQ_CLIENT_ID
    prompt: Xero CQ tenant client ID
    required_for: CQ detectors
  - name: XERO_CQ_CLIENT_SECRET
    prompt: Xero CQ tenant client secret
    required_for: CQ detectors
  - name: XERO_CQ_TENANT_ID
    prompt: Xero CQ tenant UUID
    required_for: CQ detectors
optional_environment_variables:
  - name: HERMES_FINDINGS_DATABASE_URL
    prompt: Fallback name for the findings DB connection string.
  - name: TAX_BAS_CYCLE_SC
    prompt: "BAS cycle for SC: 'quarterly' (default) or 'monthly'."
  - name: TAX_BAS_CYCLE_CQ
    prompt: "BAS cycle for CQ: 'quarterly' (default) or 'monthly'."
  - name: TAX_DUE_DATE_CRITICAL_DAYS
    prompt: "Critical due-window in days. Default 7."
  - name: TAX_DUE_DATE_WARNING_DAYS
    prompt: "Warning due-window in days. Default 30."
  - name: TAX_GST_CASH_WARNING_PCT
    prompt: "GST cash-set-aside coverage warning floor (0..1). Default 0.8."
  - name: TAX_GST_CASH_CRITICAL_PCT
    prompt: "GST cash-set-aside coverage critical floor (0..1). Default 0.5."
  - name: TAX_LOOKBACK_DAYS
    prompt: "Journal lookback window in days. Default 120 (covers a quarterly BAS)."
  - name: TAX_UNTAGGED_NET_THRESHOLD_AUD
    prompt: "Min net AUD before an untagged GST tax-type aggregate gets flagged. Default 1000."
  - name: XERO_SC_GST_ACCOUNT_CODES
    prompt: "Comma-separated Xero account codes treated as SC's GST liability/clearing accounts. Optional — defaults to the auto-detected SystemAccount=GST control account."
  - name: XERO_CQ_GST_ACCOUNT_CODES
    prompt: "Same as above for CQ."
  - name: XERO_SC_PAYG_ACCOUNT_CODES
    prompt: "Comma-separated Xero account codes treated as SC's PAYG withholding clearing accounts."
  - name: XERO_CQ_PAYG_ACCOUNT_CODES
    prompt: "Same as above for CQ."
  - name: XERO_SC_SUPER_ACCOUNT_CODES
    prompt: "Comma-separated Xero account codes treated as SC's super clearing accounts."
  - name: XERO_CQ_SUPER_ACCOUNT_CODES
    prompt: "Same as above for CQ."
  - name: XERO_SC_WAGES_ACCOUNT_CODES
    prompt: "Comma-separated Xero account codes treated as SC's gross-wages expense accounts (used by payroll-tax threshold detector)."
  - name: XERO_CQ_WAGES_ACCOUNT_CODES
    prompt: "Same as above for CQ."
  - name: TAX_PAYROLL_TAX_GROUPED
    prompt: "Set to 'true' if SC + CQ are grouped for QLD payroll-tax purposes. Default false (per-entity threshold)."
---

# jbc-tax-compliance — daily detector skill

Assumes the `jbc-context` skill is in scope: entities SC and CQ, restricted
routing, read-mostly, **escalate-never-act**. This skill never writes to
Xero, never lodges with the ATO, never touches MYOB.

## When this runs

Once a day at 07:00 AEST (`0 21 * * *` UTC). The detectors are
period-aware — BAS due dates, super quarter ends — and emit the right
severity as deadlines approach, so a single daily tick is sufficient.

## What it does

For each entity (SC and CQ — strictly independent) it pulls a Xero
read-only snapshot and runs:

```
Domain — gst
  gst-position             (info, per-entity, live net GST for current BAS period)
  gst-coding-anomaly       (warning, untagged tax-type lines above threshold OR
                            OUTPUT lines whose implied rate ≠ 10%)
  gst-cash-shortfall       (warning @ <80% coverage, critical @ <50%)

Domain — bas
  bas-deadline             (warning @ <=30 days, critical @ <=7 days)

Domain — payg
  payg-clearing-position   (info, Xero PAYG-withholding clearing balance)

Domain — super
  super-clearing-position  (info, Xero super-clearing balance)

Domain — payroll-tax
  payroll-tax-threshold    (warning when wages-lookback breaches QLD threshold)
```

Per-entity isolation is **statutory** — SC and CQ are separate taxpayers
and findings are never consolidated. The single exception allowed by
SCHEMA.md is `entity_code="GROUPED"` on `payroll-tax` only when
`TAX_PAYROLL_TAX_GROUPED=true` is set explicitly.

## Invocation

```
python3 /data/hermes/skills/jbc-tax-compliance/scripts/run_tax_compliance.py
```

Exit code 0 on success (including "ok with findings"), non-zero on hard
failure. The run row gets `status='ok' | 'exceptions' | 'failed'`.

## Hard rules

1. **Read-only on Xero.** No writes anywhere. No ATO/QRO lodgement. No
   journal posting. If a tax position requires a journal, that is the
   external accountant's job — not this skill's.
2. **`source_agent = 'tax-compliance'`** for every row written.
3. **`is_people_flag = false`** always. Tax findings are entity-level.
   If a super-shortfall ever needs to name an individual, that belongs
   in `jbc-payroll-labour`, not here.
4. **Per-entity fan-out.** Every detector iterates SC then CQ
   independently. `entity_code` is `SC` or `CQ` (or `GROUPED` only on
   `payroll-tax-threshold` when explicitly configured).
5. **Hardcoded ruleset constants.** ATO GST rate, SG rate, QLD
   payroll-tax rate + threshold, and the BAS calendar live in
   `scripts/jbc_tax_rulesets.py`. This is a deliberate Phase 2
   simplification — the legacy agent stored a versioned `TaxRuleSet`
   table; we substitute hardcoded values for the current period and
   bump the file when statutory values change. See module docstring
   for the exact source + effective-from date.
6. **Dedup via `evidence.dedupKey`.** Convention:
     `gst-position:<entity>:<periodLabel>`
     `gst-coding-anomaly:<entity>:<taxType>`
     `gst-cash-shortfall:<entity>:<periodLabel>`
     `bas-deadline:<entity>:<periodLabel>`
     `payg-clearing-position:<entity>:<isoDate>`
     `super-clearing-position:<entity>:<isoDate>`
     `payroll-tax-threshold:<entityOrGROUPED>:<periodLabel>`

## Deliberately skipped (vs the source `tax-compliance-agent`)

- **AI / Anthropic narrative pass.** `ai_explanation` is left NULL.
- **ATO Integrated Client Account recon.** Source data not available
  programmatically — out of scope.
- **MYOB Advanced reads** (PAYG W2, gross wages, super batches). MYOB
  has no API; the skill is Xero-only. PAYG and super detectors emit
  position-from-Xero-GL, not a payroll-vs-GL variance.
- **BasPreparation / GstPosition / PayrollTaxPosition side-tables**
  from the legacy Prisma schema. Findings + audit_runs is enough for
  Mark.
- **Versioned `TaxRuleSet` rows.** Hardcoded module substitutes — see
  rule 5.
- **Email / SMS / dashboard pages.** Mark handles delivery.

## Files

```
jbc-tax-compliance/
  SKILL.md                       # this file
  scripts/
    run_tax_compliance.py        # orchestrator + DB writer
    xero_tax.py                  # READ-ONLY Xero helpers
    jbc_tax_rulesets.py          # hardcoded ATO/QLD constants
    detectors/
      __init__.py
      gst.py                     # gst-position, gst-coding-anomaly, gst-cash-shortfall
      bas.py                     # bas-deadline
      payg.py                    # payg-clearing-position
      super_.py                  # super-clearing-position
      payroll_tax.py             # payroll-tax-threshold (with grouping support)
```
