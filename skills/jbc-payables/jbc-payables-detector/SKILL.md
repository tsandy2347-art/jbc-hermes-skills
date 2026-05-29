---
name: jbc-payables-detector
description: JBC payables detector. READ-ONLY against Xero (SC + CQ tenants). Sweeps ACCPAY invoices and supplier contacts to emit findings for the 14 payables detectors defined in SCHEMA.md §4. Writes findings + an audit_runs row to the shared JBC findings DB. Sibling of `create-draft-bill` (the write skill); this skill never writes to Xero. Replaces the detector half of the legacy `payables-agent` Next.js Railway service. Invoked by `hermes cron`.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, payables, finance, xero, ap, suppliers, validation, payment-run]
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
  - name: PAYABLES_DUPLICATE_WINDOW_DAYS
    prompt: "Window (days) for duplicate-invoice detection. Default 60."
  - name: PAYABLES_PAYMENT_LEAD_DAYS
    prompt: "Lead time (days) for payment-run-proposed eligibility. Default 3."
  - name: PAYABLES_NEW_SUPPLIER_WINDOW_DAYS
    prompt: "Window (days) after Contact UpdatedDateUTC during which a supplier is considered 'new' for the new-supplier-quarantine flag. Default 30."
  - name: PAYABLES_APPROVAL_PENDING_DAYS
    prompt: "Age (days) at which a DRAFT ACCPAY bill is surfaced as approval-pending. Default 2."
  - name: PAYABLES_LOOKBACK_DAYS
    prompt: "How far back to pull ACCPAY invoices (days). Default 90."
  - name: PAYABLES_INSTRUCTION_PHRASES
    prompt: "Pipe-separated list of instruction-like phrases to flag. Default includes 'pay immediately', 'urgent payment', 'new account', 'update bank', 'wire transfer'."
---

# jbc-payables-detector — daily payables detector skill

Assumes `jbc-context` is in scope: entities SC and CQ, restricted routing,
read-mostly, escalate-never-act. **This skill never writes to Xero.** The
sibling skill `jbc-payables/create-draft-bill` owns the one sanctioned
write path (DRAFT ACCPAY bill creation) and is independent of this skill.

## When this runs

Daily under `hermes cron`. Suggested schedule: `15 21 * * *` UTC
(07:15 AEST, fifteen minutes after reconciliation so we don't hammer the
Xero token endpoint simultaneously).

## What it does

For each entity (SC and CQ) it pulls a Xero ACCPAY snapshot
(read-only OAuth2 client-credentials) plus the supplier contact list,
and runs four detector groups:

```
Group A — validation (per ACCPAY bill in window)
  instruction-like-text     (critical) — reference/desc contains "pay now", "new account", ...
  no-abn                    (warning)  — supplier has no TaxNumber and an open bill
  invalid-abn               (warning)  — supplier TaxNumber fails ATO checksum
  gst-inconsistent          (warning)  — SubTotal + TotalTax != Total (2c tolerance)
  duplicate-invoice         (critical) — same supplier, same InvoiceNumber OR (amount±0.01 & date±1d)

Group B — supplier
  new-supplier-quarantine   (critical) — supplier Contact created/updated inside PAYABLES_NEW_SUPPLIER_WINDOW_DAYS
                                         AND has at least one ACCPAY bill in the window

Group C — approval
  approval-pending          (info)     — ACCPAY bill in DRAFT older than PAYABLES_APPROVAL_PENDING_DAYS

Group D — payment-run
  payment-run-proposed      (info)     — AUTHORISED + unpaid + due within PAYABLES_PAYMENT_LEAD_DAYS
                                         (proposal is informational; this skill never releases)
```

Every run inserts an `audit_runs` row at start, writes 0..N rows
into `findings` (with `evidence.dedupKey` for idempotency), and updates
the audit_runs row with counters + status at end. Schema:
`~/Finance/jbc-hermes-skills/SCHEMA.md`.

## Invocation

```
python3 /data/hermes/skills/jbc-payables-detector/scripts/run_payables_detector.py
```

Exit code 0 on success (including "ok with findings"), non-zero on hard
failure.

## Install on the runtime

```
hermes skills install jbc-payables-detector
hermes cron create --schedule "15 21 * * *" --skill jbc-payables-detector \
  --command "python3 /data/hermes/skills/jbc-payables-detector/scripts/run_payables_detector.py"
```

## Hard rules

1. **Read-only on Xero.** No POST / PUT / DELETE. Drafts are exclusively
   the job of the sibling `create-draft-bill` skill.
2. **`source_agent = 'payables'`** on every row written.
3. **`is_people_flag = false` always.** Payables never names people.
   Vendor contact names are not people-flag.
4. **Per-entity fan-out.** All Group A/B/C/D detectors run twice — once
   per tenant. There is no `BOTH` / `consolidated` entity_code emitted.
5. **Dedup via `evidence.dedupKey`.** Re-runs must not duplicate. Key
   conventions:
     `instruction-like-text:<entity>:<xeroInvoiceId>`
     `no-abn:<entity>:<xeroContactId>`
     `invalid-abn:<entity>:<xeroContactId>`
     `gst-inconsistent:<entity>:<xeroInvoiceId>`
     `duplicate-invoice:<entity>:<xeroInvoiceId>`
     `new-supplier-quarantine:<entity>:<xeroContactId>`
     `approval-pending:<entity>:<xeroInvoiceId>`
     `payment-run-proposed:<entity>:<isoDate>`
6. **Severity vocabulary stays in `critical | warning | info`.**

## PITFALLS

1. **Duplicate-invoice runs against Xero only**, not against the agent
   DB the legacy app maintained. If two invoices for the same supplier
   were entered in Xero independently, we catch them; if one was
   intercepted at email-extraction time before reaching Xero, we don't
   see it (that path was the ingest layer we deliberately skipped).
2. **New-supplier-quarantine uses Contact `UpdatedDateUTC` as a proxy**
   for creation. Xero does not surface a clean "created at" on its
   Contacts response. Window-based, with dedupKey so the same supplier
   doesn't re-fire daily once a human resolves it.
3. **ABN field**: Xero's Contact `TaxNumber` is the ABN for AU
   organisations. Some configs put it in `AccountNumber` instead. We
   only check `TaxNumber`.
4. **Instruction phrases** are matched case-insensitively against the
   bill's `Reference` and concatenated `LineItem.Description` text.
   List is overridable via `PAYABLES_INSTRUCTION_PHRASES`.
5. **Payment-run-proposed** is informational here — it does NOT batch
   anything, does NOT call the write skill, does NOT mutate Xero. The
   legacy app's `PaymentRunBatch` table is not replicated; the
   dedupKey keyed on today's ISO date keeps Mark's dashboard clean
   while still emitting fresh daily proposals as humans release the
   prior day's batch.
6. **GST tolerance is 2c** — matches legacy `validation.ts`.
7. **Approval-pending** uses the Xero bill's `Date` field, not when
   the draft was created in Hermes — it's a proxy that overstates by
   at most a few hours. Good enough for a "this draft is going stale"
   nudge.
8. **Lookback window** (`PAYABLES_LOOKBACK_DAYS`, default 90) bounds
   every ACCPAY scan. Older drafts and authorised bills are not
   re-evaluated.
9. **Best-effort per detector group.** A 5xx from Xero on one endpoint
   becomes an `ingest-failure` finding scoped to that group, not a
   whole-run crash.

## Deliberately skipped (TODOs from the source agent)

These were in the legacy `payables-agent` but require state /
integrations we do not (yet) carry into Hermes:

- **Inbound email ingestion + extraction** (`lib/payables/extraction.ts`).
  Needs the JBC helpdesk feed + Anthropic OCR pass. Will likely become
  a separate `jbc-payables-ingest` skill if/when wired.
- **`low-extraction-confidence`** — depends on the OCR pass above.
- **`new-supplier-bank-detail`** — the legacy detector compared the
  masked bank string parsed off the invoice email against a
  `SupplierBankSnapshot` history table. Neither the email parse nor
  the snapshot table is in scope. Bank-detail-change detection on
  Xero contact records is already covered by `jbc-controls-audit`
  (`bank-detail-change` detector).
- **`low-coding-confidence`** — depends on the coding engine
  (`lib/payables/coding.ts`) which scores GL/tax/tracking
  suggestions. The detector skill doesn't code.
- **`xero-draft-failed`** — emitted by the write skill on the call
  site, not here.
- **`supplier-quarantined-post-draft`** — depends on an internal
  quarantine state table that we did not port. Approximated today by
  `new-supplier-quarantine` re-firing as long as the supplier remains
  inside the new-supplier window.
- **AI enrichment** (`enrichWithAi` Anthropic batch) — `ai_explanation`
  is left NULL. Add later when needed.
- **Critical-finding SMS, daily/weekly SES reports** — Mark and the
  brief-builder cover this end of the pipeline now.
- **`PaymentRunBatch` write + invoice flip to `in-payment-run`** —
  this is the legacy app's persistent state. Out of scope; we emit
  the `payment-run-proposed` info finding only.

The full SCHEMA.md §4 vocabulary (14 detector codes) is honoured in
this skill: 8 codes are actively emitted (`instruction-like-text`,
`no-abn`, `invalid-abn`, `gst-inconsistent`, `duplicate-invoice`,
`new-supplier-quarantine`, `approval-pending`, `payment-run-proposed`)
plus `ingest-failure` when a detector group blows up. The remaining
5 codes are explicitly skipped above and reserved for future skills.

## Files

```
jbc-payables-detector/
  SKILL.md                          # this file
  scripts/
    run_payables_detector.py        # orchestrator + DB writer
    xero_client.py                  # READ-ONLY OAuth + endpoint helpers
    abn.py                          # ATO ABN checksum
    detectors/
      __init__.py
      validation.py                 # Group A
      supplier.py                   # Group B
      approval.py                   # Group C
      payment_run.py                # Group D
```
