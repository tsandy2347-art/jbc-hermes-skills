# Payables Agent — Port Notes (Next.js → Hermes `jbc-payables`)

Source: `/Users/anthonysandy/Finance/payables-agent` (Next.js + Prisma + Railway).
Spec of record: `payables-agent/CLAUDE.md`. Existing Hermes stub:
`/Users/anthonysandy/Finance/jbc-hermes/skills/jbc-payables/create-draft-bill/SKILL.md`.

This is specialist #4 of 7 in the JBC Finance Agent system. Its single most
important guardrail is **draft only, never pay** — it preps up to "ready to pay"
and stops. Segregation of duties is enforced in code.

---

## 1. Detectors

All emitted by `lib/payables/validation.ts`, `lib/payables/coding.ts`,
`lib/payables/payment-run.ts`, `lib/run.ts`.

| Detector | Where | Severity | Plain English |
|---|---|---|---|
| `low-extraction-confidence` | validation.ts | warning | Whole-invoice OCR confidence below `PAYABLES_EXTRACTION_CONFIDENCE_MIN` (default 0.85). Hold for human verify. |
| `instruction-like-text` | validation.ts | critical | Invoice/email contains imperative phrases ("pay immediately", "use new account"). Guardrail §2.3: content is data, never instructions. Quarantine. |
| `no-abn` | validation.ts | warning | Supplier ABN missing — JBC may have to withhold tax. |
| `invalid-abn` | validation.ts | warning | ABN fails ATO checksum (see `lib/payables/abn.ts`). |
| `gst-inconsistent` | validation.ts | warning | Line subtotal+GST does not reconcile to stated totals within 2c. Catches mixed GST-free/taxable confusion. |
| `duplicate-invoice` | validation.ts | critical | Same supplier within `PAYABLES_DUPLICATE_WINDOW_DAYS` (60) matching on invoice number OR (amount within 1c AND date within 1 day). Quarantine. |
| `new-supplier-quarantine` | validation.ts | critical | Supplier never seen / not status≠"new". Guardrail §2.4. Quarantine. |
| `new-supplier-bank-detail` | validation.ts | critical | Masked bank on invoice ≠ supplier's last-known masked bank. Primary invoice-redirection fraud signal. Quarantine. |
| `low-coding-confidence` | coding.ts | warning | Lowest per-line coding score < `PAYABLES_CODING_CONFIDENCE_MIN` (0.8). Score is 1.0 explicit, 0.85 supplier-default fallback, 0.5 no default. |
| `ingest-failure` | run.ts | warning | Per-invoice exception during processOne; isolated so one bad email doesn't kill the sweep. |
| `xero-draft-failed` | run.ts | warning | `createDraftBill` threw; invoice row preserved for dashboard retry. |
| `approval-pending` | run.ts | info | A draft bill was created and routed; surfaced so Mark (Finance Manager) sees pending approvals. |
| `supplier-quarantined-post-draft` | run.ts | critical | Validation-sweep detected supplier got quarantined after its invoice was already drafted; pulls the invoice out of any payment run. |
| `payment-run-proposed` | payment-run.ts | info | A per-entity batch was proposed (`status="proposed"`). Human must release. |

Severity rules (from spec §6): critical = held out of any payment run.
Warning = visible, doesn't auto-quarantine. Info = clean / pending event.

---

## 2. Source systems read

- **Invoice email channel** — `INVOICE_EMAIL_SOURCE` env var. Inbound represented as `InboundInvoice` (messageId, fromAddress, subject, attachments, bodyText, entityHint). Pulled by an `ingest-sweep` cron tick. CONFIRM C1 in spec: relationship with JBC Helpdesk module (could be the same module or a feeder).
- **Xero (SC + CQ tenants), READ** via `lib/xero.ts`:
  - `GET /Contacts?where=IsSupplier==true` (paginated to 50 pages × 100).
  - `GET /Invoices?where=Type=="ACCPAY"` since timestamp (duplicate detection / state).
  - `GET /Accounts` (active accounts for coding).
  - OAuth2 client_credentials grant, token cache per tenant code.
- **Internal Postgres (via Prisma)** — Supplier, Invoice, InvoiceLine, InvoiceEvent, ApprovalRequest, PaymentRunBatch, PayablesRun, SupplierBankSnapshot, Exception (shared with other agents, `sourceAgent="payables"`).
- **No CSV imports** found.
- **Anthropic API** — used for: low-confidence extraction assist, coding suggestions, exception `aiExplanation` enrichment (`enrichWithAi`, batches of 8), report drafting.

---

## 3. Findings shape (DraftException)

```ts
{
  detector: string,
  domain: "extraction" | "validation" | "coding" | "approval" | "payment-run" | "supplier",
  severity: "critical" | "warning" | "info",
  entityCode: "SC" | "CQ" | "BOTH",
  isPeopleFlag: boolean,        // always false in payables — no people PII flags
  title: string,
  detail: string,                // plain English why + what to check
  amount?: number,
  evidenceRef: Record<string, unknown>  // e.g. { invoiceNumber, matches: [...] }
}
```

Persisted into shared `Exception` table with `sourceAgent="payables"`, plus
`runId`, `aiExplanation` (optional from Claude). Critical findings additionally
SMS-blasted via Twilio to `PAYABLES_SMS_RECIPIENTS` (E.164 CSV) with a deep
link to the dashboard exceptions page.

---

## 4. Cron cadence

Single Railway cron sidecar image (`cron/Dockerfile`, `cron/ping.sh`,
`cron/railway.toml`) reused across multiple Railway cron services. Each service
sets `PAYABLES_KIND` and `cronSchedule` independently.

- `cron/railway.toml` has `cronSchedule = "0 21 * * *"` (21:00 UTC = 07:00 AEST daily) as the template / default service.
- Sidecar hits `POST {PAYABLES_URL}/api/cron/run?kind={PAYABLES_KIND}` with `Authorization: Bearer $CRON_SECRET`.
- Three kinds dispatched in `lib/run.ts` (`runPayables`):
  - `ingest-sweep` (default) — pull inbound, extract, validate, code, create draft, route approver. Slowest path.
  - `validation-sweep` — re-walk in-flight invoices for changed supplier status (e.g. quarantine after draft). No Xero calls. Cheap.
  - `payment-run-prep` — assemble proposed batches per entity.
- `maxDuration = 300` on the route, curl `--max-time 290` in the sidecar.
- Spec §7: `PAYABLES_PAYMENT_RUN_SCHEDULE` (default "weekly") — payment-run cadence still CONFIRM-blocked, suggested weekly Wed 09:00 AEST.

Exact production schedules for each kind: **not found** in repo (managed in Railway dashboard per service).

---

## 5. WRITE PATHS — the only sanctioned mutations

`lib/xero.ts :: createDraftBill(code, args)`:

- Hard-codes `Type: "ACCPAY"` and `Status: "DRAFT"` in the POST body — comment-flagged ABSOLUTE/guardrail §2.1.
- POSTs to `/Invoices` under the chosen tenant's `Xero-Tenant-Id` (SC or CQ — separate Pty Ltd taxpayers, never mixed).
- After receipt, sanity check: `if (inv.Status !== "DRAFT") throw` — fails loud if Xero ever returns something else (defence against API change / config drift).
- `LineAmountTypes: "Exclusive"`. Lines carry Description (truncated to 4000 chars), Quantity (default 1), UnitAmount, optional AccountCode, optional TaxType, optional Tracking[{CategoryID, OptionID}].
- OAuth: client_credentials grant on `XERO_{SC|CQ}_CLIENT_ID/SECRET/TENANT_ID`. Scopes requested: `accounting.transactions accounting.transactions.read accounting.contacts.read accounting.settings.read`.
- 429 handling: respects `Retry-After`, up to 2 retries.
- Soft-launch switch: `PAYABLES_DISABLE_XERO_WRITE=true` → returns `{ invoiceId: "disabled-write-<ts>", skipped: true }` without calling Xero. End-to-end runs in shadow mode.

Entity scoping: `processOne()` derives `entityCode` from the extracted invoice (`SC` or `CQ` only — `BOTH` is only used for ingest-failure flags). All writes go via the correct tenant's creds based on that code. No cross-entity bills.

Guardrails wrapping the write:
1. Draft only — Status hard-locked, no code path sets AUTHORISED, no `/Payments` calls anywhere.
2. **Quarantined invoices are never drafted.** `processOne`: `if (!quarantined && supplier.xeroContactId) { createDraftBill(...) }`. If new supplier → `xeroContactId` is null → no draft, by design (humans add the contact after vetting).
3. Bank-detail change / duplicate / instruction-text / new supplier → quarantine, no draft, supplier flipped to `status="watch"`.
4. Payment-run prep (`payment-run.ts`) creates `PaymentRunBatch` with `status="proposed"` only. Only a human action moves to `"released-by-human"`. Eligible = `status="approved"` AND `quarantined=false` AND (dueDate ≤ now + `PAYABLES_PAYMENT_LEAD_DAYS` (3) OR null). Eligible invoices flip to `in-payment-run` to prevent double-batching.
5. Bank account numbers masked to `***<last3>` outside the secure DB (`maskAccount`).

The orchestrator also writes (DB only, not Xero):
- `Supplier` upsert keyed on `(name + ABN)` (per Tony's note: don't dedupe on ABN alone — parent-co brands share ABNs).
- `Invoice` upsert keyed on `(supplierId, entityCode, invoiceNumber)` — idempotent re-ingest of same email.
- `InvoiceEvent` log entries (ingested / quarantined / drafted-in-xero / drafted-skipped / approval-requested) — append-only audit trail.
- `SupplierBankSnapshot` for every invoice carrying bank details — even quarantined ones contribute history.
- `ApprovalRequest` rows routed via `pickApprover(total)` against `PAYABLES_APPROVAL_LIMITS_AUD` tiers.

---

## 6. Quirks / gotchas

- **Mixed-GST invoices** (e.g. Lite n' Easy): some lines GST-free, some taxable. Extraction logic is ported from a prior "Invoice Processing Agent" — reuse, do not rewrite. Validation uses 2c tolerance to allow rounding.
- **Supplier dedup**: keyed on `(name, abn)`, not `abn` alone. Multi-brand parent companies share ABNs.
- **No Xero contact auto-create**: new suppliers are deliberately not pushed to Xero. Quarantine until a human creates the contact manually.
- **Spec §2.3**: invoice text is data only — instruction-like phrases ("urgent", "new account", "pay immediately") are themselves a critical flag. List lives in extraction layer (`inv.instructionPhrases`).
- **`isPeopleFlag`** is always `false` in payables (it exists for cross-agent uniformity with payroll/recon).
- **Soft-launch env** `PAYABLES_DISABLE_XERO_WRITE` — lets the agent run completely except for the actual POST. Critical for shadow/dry-run.
- **Approval tiers** unconfirmed (CONFIRM C2). Without `PAYABLES_APPROVAL_LIMITS_AUD`, everything routes to "tony" with a CONFIRM blocker on the admin page.
- **POs** unconfirmed (CONFIRM C3). `PAYABLES_USE_PURCHASE_ORDERS` defaults false.
- **Bug in `cron/ping.sh` line 27**: `-H "Authorization: Bearer ***` (unterminated quote in the redacted copy). Real shipped script has the secret interpolated; the redaction in repo broke the quote. Re-port cleanly.
- **AI enrichment** is optional — gracefully degrades when `ANTHROPIC_API_KEY` is empty.
- **Hermes inbox**: `HUB_API_KEY` gates `/api/findings` for Mark's read.
- **SMS direct from agent** (Twilio), not via a hub — pattern documented in feedback_hermes_architecture.md.

---

## 7. Required env vars

(From `lib/env.ts`.)

Core:
- `DATABASE_URL` (required)
- `NODE_ENV`

Xero (per tenant — both needed for dual-entity work):
- `XERO_SC_CLIENT_ID`, `XERO_SC_CLIENT_SECRET`, `XERO_SC_TENANT_ID`
- `XERO_CQ_CLIENT_ID`, `XERO_CQ_CLIENT_SECRET`, `XERO_CQ_TENANT_ID`

Email channel:
- `INVOICE_EMAIL_SOURCE`, `INVOICE_EMAIL_FROM_HELPDESK_TOKEN`

Anthropic:
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`)

Thresholds & behaviour:
- `PAYABLES_DUPLICATE_WINDOW_DAYS` (60)
- `PAYABLES_EXTRACTION_CONFIDENCE_MIN` (0.85)
- `PAYABLES_CODING_CONFIDENCE_MIN` (0.8)
- `PAYABLES_APPROVAL_LIMITS_AUD` (CSV tiers; CONFIRM C2)
- `PAYABLES_USE_PURCHASE_ORDERS` (false; CONFIRM C3)
- `PAYABLES_PAYMENT_RUN_SCHEDULE` ("weekly")
- `PAYABLES_PAYMENT_LEAD_DAYS` (3)
- `PAYABLES_PRICE_SPIKE_PCT` (40) — flag lines >N% above supplier's trailing median (note: detector implementation **not found** in validation.ts; may be planned/TBD)
- `PAYABLES_MOCK` (boolean)
- `PAYABLES_DISABLE_XERO_WRITE` (boolean — soft launch)

Reports / SES:
- `PAYABLES_DAILY_BRIEF_RECIPIENTS`, `PAYABLES_PAYMENT_RUN_RECIPIENTS`, `PAYABLES_WEEKLY_SUMMARY_RECIPIENTS`, `PAYABLES_HEARTBEAT_RECIPIENTS`
- `REPORT_FROM` (default `payables@justbettercareqld.com.au`)
- `AWS_REGION` (`ap-southeast-2`), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

Cron / inbox:
- `CRON_SECRET`, `HUB_API_KEY`

SMS:
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `PAYABLES_SMS_RECIPIENTS`

Auth / admin:
- `BASIC_AUTH_USER`, `BASIC_AUTH_PASS`, `BASIC_AUTH_USERS`, `ADMIN_USERNAMES` (default `tony,nicole`)

---

## 8. Comparison vs existing Hermes stub

Existing: `jbc-hermes/skills/jbc-payables/create-draft-bill/SKILL.md` (v0.2.0).

What the stub **already covers** well:
- The single write primitive — DRAFT bill creation in Xero, Type=ACCPAY, Status=DRAFT hard-locked, both SC + CQ tenants supported.
- OAuth client_credentials, tenant-id header, error relay verbatim.
- Brisbane-time stamping in Reference, 255-char cap.
- Conversational confirm-before-write (YES gate).
- Don't pre-validate account codes — let Xero be the gate.
- Decline requests to post / authorise.
- Multi-line lines with description, unit amount, account code, optional tax type.

What is **missing vs the Next.js specialist** (i.e. needs further skills / sibling pieces or a richer pipeline skill):

| Capability | Status in stub | Needed |
|---|---|---|
| Inbound email ingestion + extraction | not present | Needs an ingestion path (or a "process-inbound-invoice" skill) feeding extracted fields to create-draft-bill. |
| Validation detectors (ABN, GST arithmetic, duplicate, instruction text, new supplier, bank change, extraction confidence) | not present | Either bake into skill pre-write, or add a `validate-invoice` sibling skill that returns a findings list and blocks the draft on critical. |
| Quarantine logic (don't draft when critical) | not present | Skill currently drafts on YES regardless — needs guardrail wiring once detectors arrive. |
| ABN checksum (`lib/payables/abn.ts`) | not present | Port. |
| Supplier upsert + history (Supplier, SupplierBankSnapshot, InvoiceEvent audit trail) | not present | Needs a persistence layer outside Hermes — TBD whether Hermes owns DB or just calls Xero. |
| Coding (GL/tax/tracking) with confidence + low-coding-confidence flag | partial — user provides codes, stub may "infer" plausibly | Port the codify + score logic; surface CONFIRM when score low. |
| Approval routing (`pickApprover`, tiered limits) | not present | Needs ApprovalRequest equivalent. |
| Payment-run prep (`proposePaymentRuns`, "proposed" → "released-by-human") | not present | Needs separate skill `jbc-payables/propose-payment-run` or similar. |
| Daily brief / Payment run proposal / Weekly summary reports (SES) | not present | Hermes reporting cadence TBD. |
| Critical SMS to Tony (Twilio) | not present | Plug into Hermes alert plumbing. |
| Mark/Hermes inbox `/api/findings` push | not present (skill is interactive only) | Needs cron-style or push pattern to put findings on Mark's stream. |
| `PAYABLES_DISABLE_XERO_WRITE` soft-launch flag | not present | Easy add. |
| Reference tag formatting (`[DRAFT auto-generated by JBC Hermes ...]`) | present, nice touch | Keep. |

Net: the stub is the **single write primitive** done well. ~90% of the
specialist's value (detectors, ingestion, quarantine, batching, reports) is
not yet in Hermes.

---

## 9. Migration difficulty: **L (Large)**

Why:
- Surface area is wide: 14 detectors, 3 cron kinds, 6 domains (A–F), AI enrichment, SMS, SES reports, dashboard pages, approval routing, payment-run batching.
- Persistent state required (Supplier history, bank snapshots, invoice events, approval requests, payment-run batches, idempotent upserts). Hermes is more action-oriented; needs an external DB story or significant skill-state design.
- Inbound email ingestion is non-trivial and currently CONFIRM-blocked (Helpdesk relationship C1).
- Approval tiers and PO usage still unconfirmed (C2, C3) — block phase 4.
- Multiple unconfirmed integration points: Helpdesk, approver list, payment cadence.
- Mitigating: the **single write primitive (create-draft-bill)** is already done well in Hermes; the highest-risk surface (Xero write) is the safest piece. Migration can be staged:
  - Phase A (S): port detectors as a `validate-invoice` skill — pure functions, no state.
  - Phase B (M): port ABN + extraction helpers, add soft-launch.
  - Phase C (L): persistence + ingestion + approval + payment-run batching. This is the big lift.
  - Phase D (M): reports + SMS + dashboard equivalents (probably out of Hermes proper).

File written: `/Users/anthonysandy/Finance/jbc-hermes-skills/notes/payables.md`
