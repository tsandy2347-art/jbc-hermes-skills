---
name: jbc-receivables
description: Daily JBC accounts-receivable specialist. READ-ONLY against Xero (SC + CQ tenants). Pulls outstanding ACCREC invoices, ACCRECPAYMENTS, credit notes, and customer contacts; ages every open invoice; aggregates by debtor; runs 8 detectors across the aging / segmentation / cash-application surface. Writes findings + an audit_runs row into the shared JBC findings DB. NEVER drafts or sends email — the legacy "follow-up draft" path is intentionally dropped at this layer. Replaces the legacy `receivables-agent` Next.js Railway service. Invoked by `hermes cron` once daily at 07:00 AEST.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, receivables, finance, xero, ar, aging, debtors]
    category: jbc-finance
required_environment_variables:
  - name: JBC_FINDINGS_DATABASE_URL
    prompt: Postgres connection string for the shared JBC findings DB (falls back to HERMES_FINDINGS_DATABASE_URL if unset)
    required_for: writing findings + audit_runs
  - name: XERO_SC_CLIENT_ID
    prompt: Xero SC tenant client ID (Just Better Care Sunshine Coast Pty Ltd)
    required_for: SC detectors
  - name: XERO_SC_CLIENT_SECRET
    prompt: Xero SC tenant client secret
    required_for: SC detectors
  - name: XERO_SC_TENANT_ID
    prompt: Xero SC tenant UUID
    required_for: SC detectors
  - name: XERO_CQ_CLIENT_ID
    prompt: Xero CQ tenant client ID (Just Better Care Central Queensland Pty Ltd)
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
  - name: AR_DEBTOR_EXPOSURE_LIMIT_AUD
    prompt: "Single-debtor outstanding exposure ceiling (AUD). Default 25000."
  - name: AR_WRITEOFF_CANDIDATE_DAYS
    prompt: "Days past due before an invoice is surfaced as a write-off candidate. Default 120."
  - name: AR_UNALLOCATED_RECEIPT_AGE_DAYS
    prompt: "Grace window (days) before an unmatched ACCRECPAYMENT becomes an unallocated-receipt finding. Default 2."
  - name: AR_PAYMENT_LOOKBACK_DAYS
    prompt: "How far back to pull ACCRECPAYMENTs for cash-application checks (days). Default 180."
---

# jbc-receivables — daily detector skill

Assumes the `jbc-context` skill is in scope: entities SC and CQ, restricted
routing, read-mostly, escalate-never-act. This skill never writes to Xero
and never sends email to debtors.

## When this runs

Once a day at 07:00 AEST (`0 21 * * *` UTC), triggered by Hermes's own
cron. Install once, then register the cron entry (see Install below).

## What it does

For each entity (SC, CQ) it pulls a Xero AR snapshot (read-only OAuth2
client-credentials) — outstanding ACCREC invoices, recent
ACCRECPAYMENTs, ACCREC credit notes, and customer contacts. It ages
every outstanding invoice, aggregates by debtor, then runs 8 detectors:

```
Aging / escalation
  invoice-90-plus            (critical)
  invoice-60-plus            (warning)
  writeoff-candidate         (critical, > AR_WRITEOFF_CANDIDATE_DAYS)
  disputed-invoice           (warning; today a no-op — see PITFALLS)

Debtor segmentation
  debtor-exposure-breach     (critical, single-debtor outstanding > limit)
  deteriorating-payer        (warning; today a no-op — see PITFALLS)

Cash application
  unallocated-receipt        (warning)
  part-payment               (warning)
```

Every run inserts an `audit_runs` row at start, writes 0..N rows into
`findings` (dedup on `evidence.dedupKey`), and updates the audit_runs
row with counters + status at end. Schema:
`~/Finance/jbc-hermes-skills/SCHEMA.md`.

## Invocation

```
python3 /data/hermes/skills/jbc-receivables/scripts/run_receivables.py
```

Exit code 0 on success (including "ok with findings"), non-zero on hard
failure. A crashed mid-run leaves an `audit_runs` row Mark surfaces as
"failed: no completion".

## Install on the runtime

```
hermes skills install jbc-receivables
hermes cron create --schedule "0 21 * * *" --skill jbc-receivables \
  --command "python3 /data/hermes/skills/jbc-receivables/scripts/run_receivables.py"
```

## Hard rules

1. **Read-only on Xero.** OAuth scopes requested are `.read` only.
2. **Never sends email.** The legacy receivables-agent maintained a
   FollowUpDraft queue + a "drafts only, human sends" guardrail. That
   whole path is dropped at the skill layer. If/when JBC wants
   automated follow-up drafts again, it belongs in a separate skill
   that explicitly opts in to outbound and is reviewed against
   guardrail #1.
3. **`source_agent = 'receivables'`** for every row written.
4. **`is_people_flag = false` always.** Per port directive: receivables
   debtor names are treated as organisations / billing arrangements, not
   natural-person care records. (Different from payroll, which DOES
   flag people.)
5. **Per-entity fan-out.** Every detector runs twice — once per tenant.
   `entity_code` is `SC` or `CQ`. `consolidated` is not used.
6. **Domain.** All findings use `domain = 'ar'` (or `domain = 'ingest'`
   for detector-failed fallbacks) per SCHEMA §5.
7. **Dedup via `evidence.dedupKey`.** Key conventions:
     `invoice-90-plus:<entity>:<xeroInvoiceId>`
     `invoice-60-plus:<entity>:<xeroInvoiceId>`
     `writeoff-candidate:<entity>:<xeroInvoiceId>`
     `part-payment:<entity>:<xeroInvoiceId>`
     `disputed-invoice:<entity>:<xeroInvoiceId>`
     `debtor-exposure-breach:<entity>:<xeroContactId>`
     `deteriorating-payer:<entity>:<xeroContactId>`
     `unallocated-receipt:<entity>:<xeroPaymentId>`

## PITFALLS

1. **No internal baseline state.** The legacy app kept `Debtor.avgDays
   LateRecent / Baseline` rows to drive deteriorating-payer. We don't
   carry that side table — the detector ships as a no-op stub. When
   baseline carry-forward lands (future skill), wire it back on. Code
   location: `detectors/debtors.py::detect_deteriorating_payer`.
2. **Disputed flag has no Xero surface.** Legacy disputed-invoice
   detector read `ARInvoice.status == 'disputed'` from the internal DB,
   set manually via the /exceptions UI. We don't have that surface
   here, so the detector ships as a no-op. The function is wired so
   that when manual dispute marking returns (e.g. via a Mark resolution
   note convention), it can be turned on quickly.
3. **DRAFT invoices excluded.** `listOutstandingSalesInvoices` filters
   out `Status==DRAFT` to match legacy behaviour — DRAFT invoices
   aren't owed yet.
4. **Unallocated receipt grace.** A payment counts as unallocated only
   when it has no `Invoice.InvoiceID` link OR the linked invoice is no
   longer in the outstanding pull AND the payment is older than
   `AR_UNALLOCATED_RECEIPT_AGE_DAYS` (default 2). Fully-paid
   reconciliations don't trip the detector because their invoice has
   left the outstanding pull but the payment date passes the grace.
5. **`unallocated-receipt` false positives.** If the lookback window
   has rolled past a payment whose invoice has long-since been paid +
   archived, that payment will linger in the lookback. Default
   `AR_PAYMENT_LOOKBACK_DAYS=180` is balanced; raise with care.
6. **DSO / trend tables not ported.** The legacy
   `ReceivablesSnapshot` time-series is intentionally omitted — Mark
   derives trend from `findings` + `audit_runs`.
7. **Best-effort per detector.** Each detector is wrapped in try/except
   in the orchestrator. A failure emits an `ingest`-domain finding
   tagged with the detector name; it does not abort the run.
8. **Per-tenant fan-out.** If one tenant's Xero credentials are
   missing, that tenant is skipped silently — same convention as the
   recon skill.
9. **Tenant rate limits.** Xero rate-limits per tenant; pulls are
   sequential within a tenant. The 429-retry helper in `xero_ar.py`
   handles short backoffs.

## Deliberately skipped (TODOs from the source agent)

- **FollowUpDraft cadence + AI drafter (`draftFollowUp`).** Drafts-only
  email path dropped at the skill layer per port directive.
- **AWS SES daily/weekly/heartbeat reports + Twilio SMS.** Mark and the
  brief-builder cover that end of the pipeline now.
- **`Debtor` / `ARInvoice` / `ReceivablesSnapshot` /
  `UnallocatedReceipt` side tables.** Replaced with `dedupKey`-based
  idempotency on the shared `findings` table.
- **Baseline carry-forward.** See PITFALL #1.
- **Mock-mode (`AR_MOCK=true`).** Out of scope; the skill expects
  real Xero credentials.

## Files

```
jbc-receivables/
  SKILL.md                       # this file
  scripts/
    run_receivables.py           # orchestrator + DB writer
    xero_ar.py                   # READ-ONLY OAuth + AR endpoint helpers
    detectors/
      __init__.py
      aging.py                   # 90+, 60+, writeoff, part-payment, disputed (stub)
      debtors.py                 # debtor-exposure-breach, deteriorating-payer (stub)
      cash.py                    # unallocated-receipt
```
