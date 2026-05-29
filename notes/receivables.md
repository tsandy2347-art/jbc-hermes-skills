# Receivables Agent — porting notes (jbc-receivables)

Source: /Users/anthonysandy/Finance/receivables-agent
Original: Next.js (App Router) + TypeScript + Prisma/Postgres on Railway.
Spec: CLAUDE.md sections 1–11. Specialist agent #6 of 7. Owns AR — aging, follow-up drafting (human sends), cash application, write-off candidates.

## 1. Detectors

All live under lib/ar/detectors/. Registered in lib/ar/detectors/index.ts. Each is a pure function over a `DetectorContext` returning `DraftException[]`.

| Code | Domain | Plain English | Severity |
|---|---|---|---|
| invoice-90-plus | escalation | Any invoice past 90 days. Hard-critical for human attention. | critical |
| invoice-60-plus | escalation | Invoice past 60 days but not yet 90. Firm warning. | warning |
| debtor-exposure-breach | escalation | A single debtor's total outstanding exceeds `AR_DEBTOR_EXPOSURE_LIMIT_AUD` (concentration risk). | critical |
| writeoff-candidate | escalation | Invoice older than `AR_WRITEOFF_CANDIDATE_DAYS` (default 120). Surfaced for human decision — agent never writes off. | critical |
| disputed-invoice | escalation | Invoice flagged as disputed (debtor query). | warning |
| deteriorating-payer | segmentation | Debtor's recent average days-late has grown vs their own baseline by ≥ `AR_DETERIORATION_PCT` (default 50%). Early warning before they hit 90+. | warning |
| unallocated-receipt | cash-application | Payment landed in Xero with no invoice link, older than `AR_UNALLOCATED_RECEIPT_AGE_DAYS` (default 2 days). | warning |
| part-payment | cash-application | Invoice with `amountPaid > 0` but still outstanding. Needs human follow-up to chase balance or apply credit. | warning |

Plus one non-detector emission: `follow-up-drafted` (info) — written by the cadence step whenever a FollowUpDraft is queued, so drafts also appear in the findings list.

Cadence stages (lib/ar/cadence.ts): reminder (at due date), firm (+30d), escalation (+60d), statement (debtor with ≥ `AR_STATEMENT_MIN_INVOICES` open invoices = single combined statement). Cooldown between same-stage redrafts: `AR_REDRAFT_COOLDOWN_DAYS` (7).

## 2. Source systems read

- Xero — both tenants, SC and CQ — READ-ONLY OAuth2. Required scopes: `accounting.transactions.read`, `accounting.contacts.read`, `accounting.settings.read`. Pulled: outstanding sales invoices, payments, credit notes, debtor contacts. Code: lib/xero.ts, lib/ar/snapshot.ts.
- Internal Postgres (Prisma) — prior Debtor rows used to carry forward `avgDaysLateRecent` / `avgDaysLateBaseline` (see `mergeBaselinesFromPriorDebtors` in lib/run.ts). Prior FollowUpDraft rows used for cadence cooldown.
- Revenue & Claims Agent handoff — spec says short-paid / rejected-then-invoiced claims flow in via shared DB (internal handoff). In current code this is referenced but the handoff reader isn't fully wired beyond Xero (Phase 5 territory).
- Mock mode: `AR_MOCK=true` → skips Xero entirely, loads lib/ar/mock.ts canned snapshot. Useful for porting / dry-runs.
- No CSV ingestion.

## 3. Findings emitted

Written to the shared `Exception` model with `sourceAgent = "receivables"`.

Shape (DraftException → Exception row):
```
sourceAgent:   "receivables"
runId:         <auditRun.id>
detector:      <code from table above, or "follow-up-drafted">
entityCode:    "SC" | "CQ" | "BOTH"
domain:        "escalation" | "segmentation" | "cash-application" | "follow-up" | "intelligence"
severity:      "critical" | "warning" | "info"
isPeopleFlag:  false   (receivables doesn't raise people flags — field present for parity)
title:         short human headline
detail:        plain-English explanation (firm-but-respectful tone)
amount:        Decimal | null  (outstanding $ where meaningful)
aiExplanation: Claude-generated context, optional, fail-quiet
evidenceRef:   JSON  { entity, debtorId, invoiceIds, cadenceStage, ... }
```

Additional side-effect tables written each run:
- Debtor (upsert per `xeroContactId+entityCode`)
- ARInvoice (per invoice, with `ageBucket` / `status`)
- ReceivablesSnapshot (one per entity per run — totals + buckets + DSO)
- UnallocatedReceipt (rows for resolution UI)
- FollowUpDraft (queued: subject + body + invoiceIds, status=`queued` only; humans set `sent-by-human` / `discarded`)
- AuditRun (status, exceptionsCount, peopleFlagsCount=0, criticalCount, durationMs, dataSnapshot)

Severity mapping (from spec §6):
- critical: 90+ invoice, exposure breach, write-off candidate
- warning: 60-89, deteriorating payer, part-payment, unallocated receipt, disputed
- info: aging summary, DSO trend, follow-up-drafted, "all current"

## 4. Cron cadence

Railway cron — cron/railway.toml: `0 21 * * *` (UTC) = 07:00 AEST daily. Same trigger time as recon-agent so cross-agent snapshots line up.
Entry point: cron container hits `POST /api/cron/run` with `CRON_SECRET` bearer (see app/api/cron/run/route.ts, cron/ping.sh). That route calls `runReceivables()`.

Weekly AR report fires only on Mondays Brisbane local (`isWeeklyDigestDay()` in lib/run.ts) — same cron tick, gated.

For Hermes port: a single daily cron task is sufficient. The Monday-only weekly report can either be a second cron entry or kept as a gated branch in one task.

## 5. Write paths (read-only confirmation)

- **Xero: read-only.** Guardrail #3 in spec; OAuth scopes are `.read` only. Never edits invoices, applies payments, issues credit notes. Confirmed.
- **External email:** receivables NEVER emails debtors. It only drafts FollowUpDraft rows; a human in the dashboard reviews and sends.
- **Outbound the agent *does* perform:**
  - Internal reports via AWS SES to staff (Tony, Nicole) — daily brief, follow-up review queue, weekly AR report.
  - SMS via Twilio to staff on critical findings (`AR_SMS_RECIPIENTS`).
  - Heartbeat-on-failure email to staff.
  - Database writes to its own Postgres (Debtor, ARInvoice, FollowUpDraft, Exception, etc.).

Net: read-only on external systems of record. All external send actions target internal staff, not debtors.

## 6. Quirks / gotchas

- **Drafts only, never send.** The cadence step creates `FollowUpDraft.status="queued"`. Only a human action moves it to `"sent-by-human"`. The agent must never set sent status. Carry this guardrail into the Hermes port.
- **Baseline carry-forward.** `avgDaysLateRecent`/`Baseline` aren't recomputed from full payment history each run; they're carried forward from prior Debtor rows. First run = nulls = deteriorating-payer detector silent. Acceptable but worth a comment.
- **Cooldown key.** Cadence dedup key is `${entity}|${xeroContactId}|${stage}` and pulls last 5000 FollowUpDraft rows. Will need rotation/index for long-term run.
- **Unallocated receipt definition.** Receipt with `Invoice.InvoiceID` not in the open-invoice set OR no link at all, AND `received >= AR_UNALLOCATED_RECEIPT_AGE_DAYS` days ago. Short receipts get a grace window.
- **People flags are always false.** Field exists on Exception for shape parity with other agents; receivables doesn't classify staff misconduct.
- **AI enrichment is fail-quiet.** If `ANTHROPIC_API_KEY` missing or call fails, exception is still persisted; `aiExplanation` is null.
- **Detector errors don't abort run.** Each detector wrapped in try/catch; failure emits a `warning` exception tagged with the detector code.
- **Decimal handling.** Uses `decimal.js` and Prisma `Decimal`. Don't drop to JS number for currency math during port.
- **Two-tenant fan-out.** Every step iterates `["SC", "CQ"] as const`. Each tenant has its own Xero client creds. If one tenant is unconfigured (`!t.configured`) it's silently skipped — don't treat as error.
- **Tone.** Drafts are for elderly participants / family / plan managers. "Firm-but-respectful, plain register" — keep the system prompt in lib/anthropic.ts when porting `draftFollowUp`.
- **Sensitive data.** Spec §2.5 — no participant or debtor data in logs/reports beyond a masked reference. AR detail stays in the DB/UI, not in heartbeats or general reports.
- **DSO is a trend, not a single value.** Snapshot table stores per-run; trend is derived in reports.
- **Hermes findings consumer.** `GET /api/findings` gated by `HUB_API_KEY` bearer. Mark voice agent reads from there. Keep that contract.
- **Weekly digest gating** uses Australia/Brisbane Monday. Don't switch to UTC weekday during port.

## 7. Required env vars

Core
- `DATABASE_URL`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`)
- `CRON_SECRET`
- `HUB_API_KEY` (Hermes inbox bearer for /api/findings)

Xero (read-only)
- `XERO_SC_CLIENT_ID`, `XERO_SC_CLIENT_SECRET`, `XERO_SC_TENANT_ID`
- `XERO_CQ_CLIENT_ID`, `XERO_CQ_CLIENT_SECRET`, `XERO_CQ_TENANT_ID`

Cadence + thresholds (CONFIRM defaults with Tony/Nicole)
- `AR_CADENCE_REMINDER_DAYS=0`
- `AR_CADENCE_FIRM_DAYS=30`
- `AR_CADENCE_ESCALATION_DAYS=60`
- `AR_REDRAFT_COOLDOWN_DAYS=7`
- `AR_DEBTOR_EXPOSURE_LIMIT_AUD=25000` (CONFIRM)
- `AR_WRITEOFF_CANDIDATE_DAYS=120` (CONFIRM)
- `AR_DETERIORATION_PCT=50`
- `AR_STATEMENT_MIN_INVOICES=3`
- `AR_UNALLOCATED_RECEIPT_AGE_DAYS=2`

Auth (UI)
- `BASIC_AUTH_USER` + `BASIC_AUTH_PASS`, or `BASIC_AUTH_USERS=tony:pwA,nicole:pwB`
- `ADMIN_USERNAMES=tony,nicole`

Reports (SES)
- `AWS_REGION=ap-southeast-2`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `REPORT_FROM`
- `AR_DAILY_RECIPIENTS`, `AR_REVIEW_QUEUE_RECIPIENTS`, `AR_WEEKLY_RECIPIENTS`, `AR_HEARTBEAT_RECIPIENTS`

SMS (criticals only)
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
- `AR_SMS_RECIPIENTS` (CSV E.164)

Mock / dev
- `AR_MOCK=true` (skip Xero, use canned snapshot)

## 8. Migration difficulty: **M (Medium-Large, lean Large)**

Why M and not S:
- Largest data model of any agent so far — 4 receivables-specific Prisma models (Debtor, ARInvoice, FollowUpDraft, ReceivablesSnapshot, UnallocatedReceipt) plus shared Exception. Port has to preserve them or build equivalent storage for cooldown/baseline carry-forward.
- 8 detectors + cadence engine + cash-application logic — more moving parts than aging-only agents.
- AI draft-writer path (`draftFollowUp` in lib/anthropic.ts) is a real feature, not a side-quest. Tone-tuned per cohort (plan-manager / self-managed / sah-client / private). Carry the system prompt verbatim.
- Two-tenant Xero fan-out, but the existing client lib is reusable across the Hermes finance skills.
- Multiple report types (daily brief, follow-up review queue gated to Nicole, weekly Monday-only). Need report templates ported.
- SMS-on-critical + heartbeat-on-failure paths need to land too.

Why not L:
- Read-only on external systems → no transactional edge cases.
- No people-flag classification logic to port.
- Detectors are pure functions over a snapshot — easy to lift.
- Mock mode (`AR_MOCK`) gives a working dry-run from day one.

Suggested port order
1. Snapshot pull (Xero, both tenants, mock fallback) + aging engine.
2. Persist Debtor/ARInvoice/ReceivablesSnapshot + the 5 escalation detectors → Exceptions.
3. Cash-application detectors (part-payment, unallocated-receipt) + UnallocatedReceipt persistence.
4. Cadence + AI drafter + FollowUpDraft queue + `follow-up-drafted` info findings.
5. Deteriorating-payer (needs baseline carry-forward — depends on Debtor rows existing).
6. Reports (daily, queue, weekly), SMS, heartbeat.
7. Hermes /api/findings consumer parity (HUB_API_KEY bearer).

## Reference paths in source

- Orchestrator: lib/run.ts
- Detectors: lib/ar/detectors/*.ts (+ index.ts registry)
- Aging: lib/ar/aging.ts
- Snapshot pull: lib/ar/snapshot.ts, lib/xero.ts
- Cadence: lib/ar/cadence.ts
- AI: lib/anthropic.ts
- Reports: lib/reports.ts
- Email/SMS: lib/email.ts, lib/sms.ts
- Schema: prisma/schema.prisma
- Cron: app/api/cron/run/route.ts, cron/railway.toml
- Mock: lib/ar/mock.ts
- Env template: .env.example
- Spec: CLAUDE.md
