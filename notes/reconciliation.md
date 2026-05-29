# jbc-reconciliation — port notes

Source: `/Users/anthonysandy/Finance/reconciliation-agent` (Next.js App Router + Prisma + Postgres, Railway-hosted).
Authoritative spec: `CLAUDE.md` in that repo. Code is loyal to it.

Two entities, **always tagged**:
- **SC** — Just Better Care Sunshine Coast Pty Ltd (primary).
- **CQ** — Just Better Care Central Queensland Pty Ltd (the one with the overdraft history).

The orchestrator (`lib/recon/run.ts → runReconciliation()`) runs once per cron tick, pulls a snapshot per tenant in parallel, runs three detector domains plus two Stage-2 prediction engines, persists `ReconciliationRun + Exception + CashSnapshot + IntercompanyBalance + SuggestedMatch + CodingSuggestion`, then emits a daily email + SMS for criticals.

---

## 1. Detectors

All detectors live in `lib/recon/`. Each emits `DraftException { domain, severity, title, detail, amount?, evidenceRef?: { uniqueKey, ... } }` (see `lib/recon/types.ts`).

### Domain A — bank (`lib/recon/bank.ts`)
Per bank account, per entity. Gated on `BankAccount.enabledForCoding` (cash table still shows all; exceptions only fire for enabled accounts).

- **Overdraft** (`severity: critical`) — `BankSummary` closing balance < 0. Skipped for `BankAccountType == CREDITCARD` (credit cards naturally negative).
- **Low cash warning** (`warning`) — balance below `RECON_LOW_CASH_WARNING_{SC,CQ}_AUD` if configured.
- **Balance unavailable** (`critical`) — couldn't derive balance from `BankSummary` report for an enabled account.
- **Stale unreconciled GL lines** (`warning`) — unreconciled bank transactions older than `RECON_UNMATCHED_DAYS` business days (default 2). One Exception per account, rolled-up with total face value.
- Side effect: writes a `CashSnapshot` row per account per run.

Known gap: Xero `StatementLines` endpoint is access-restricted, so "bank statement line with no GL counterpart" is not checked — only the GL side.

### Domain B — intercompany (`lib/recon/intercompany.ts`)
Runs once across BOTH snapshots, attached to the SC run.

- **Intercompany codes not configured** (`critical`) — `XERO_SC_LOAN_TO_CQ_CODE` or `XERO_CQ_LOAN_FROM_SC_CODE` unset.
- **Balance not readable** (`critical`) — codes set but row not found in Trial Balance for one/both tenants.
- **Intercompany mismatch** (`critical`) — `abs(|scBalance| - |cqBalance|) > RECON_INTERCOMPANY_TOLERANCE_AUD` (default $1). The agent computes the absolute mirror gap so sign convention doesn't trip it.
- Side effect: writes `IntercompanyBalance { scSideBalance, cqSideBalance, difference, isMatched }` every run.

### Domain C — journal (`lib/recon/journals.ts`)
Per entity.

- **Unposted manual journal** (`warning`, escalates to `critical` if > 5 business days old) — one Exception per `ManualJournal.Status == "DRAFT"`. Includes Xero deep-link `https://go.xero.com/Bank/ViewManualJournal.aspx?ManualJournalID=…`, narration, amount, line count, age.
- **Late-posted journals** (`warning`) — aggregate per entity. Posted manual or GL journals where `businessDaysBetween(JournalDate, CreatedDateUTC) > RECON_JOURNAL_LAG_DAYS` (default 3). Carries `top10` worst offenders in `evidenceRef` (could be 90+, so aggregate is deliberate).
- **Large posted manual journal** (`warning`) — one Exception per `POSTED` MJ with positive-line sum ≥ `RECON_LARGE_JOURNAL_AUD` (default $10,000).

### Stage 2 helpers (not exception-emitting)
- **`runMatching` (`lib/recon/matching.ts`)** — for each unreconciled bank line, scores outstanding invoices/bills/credit-notes deterministically (amount-exact / amount-within-GST / contact / ref / date signals; GST tolerance 2c, max date gap 14 days, top 3 per line, min confidence 0.45). Persists as `SuggestedMatch` rows for Nicole to triage at `/matches`. No LLM.
- **`runCoding` (`lib/recon/coding.ts`)** — create-mode prediction: groups `ReconciledHistory` rows by `(accountCode | taxType | costCentre | location)` tuples per contact+direction, picks best-scoring sub-pattern. `CodingRule` (Nicole's explicit overrides) beats history. Persists as `CodingSuggestion`.

### AI layer (`lib/anthropic.ts → classifyException`)
After detection, every NEW (non-deduped) draft is sent to Claude (`ANTHROPIC_MODEL`, default `claude-sonnet-4-6`) to confirm/upgrade severity and produce a one-line plain-English `aiExplanation`. Dedup hits skip the LLM call.

---

## 2. Source systems read

| Source | How | File |
|---|---|---|
| **Xero SC tenant** | OAuth2 client_credentials per tenant (`XERO_SC_*`). READ-ONLY scopes: `accounting.transactions.read`, `accounting.journals.read`, `accounting.reports.read`, `accounting.settings.read`. Endpoints used: `Accounts` (bank only), `BankTransactions` (unreconciled, paginated, sinceDate-filtered), `ManualJournals`, `Journals` (last 100), `Reports/TrialBalance`, `Reports/BankSummary`, `Invoices` (ACCREC/ACCPAY AUTHORISED), `CreditNotes`. | `lib/xero.ts`, `lib/recon/pull.ts` |
| **Xero CQ tenant** | Same as SC, second tenant. `XERO_CQ_*`. | same |
| **Postgres (own DB)** | Prisma. Reads `BankAccount.enabledForCoding`, `ReconciledHistory`, `CodingRule`, `Exception` (for dedup). | `lib/prisma.ts` |
| **CSV bank statement uploads** | Nicole uploads via `/coding/import`. `lib/csv-parser.ts` auto-detects Xero / Westpac / AMEX / generic shapes. Persists `ImportBatch` + `StatementLine`, runs `runCoding` against them. | `lib/import-statements.ts`, `lib/csv-parser.ts` |
| **MYOB** | NOT read by an integration. Payroll journals enter via a paste/upload flow that builds a draft MJ (`lib/recon/payroll-journal.ts`). MYOB column math is replicated in TS. | `lib/recon/payroll-journal.ts` |
| **AlayaCare** | Not referenced anywhere in this repo. | — |

`RECON_MOCK=true` swaps the entire Xero layer for `lib/recon/mock.ts` fixtures + `mock-seed.ts` reference data.

---

## 3. Findings emitted (HUB API shape)

Exposed by `GET /api/findings` (Bearer `HUB_API_KEY` or `x-api-key`). Mapping in `lib/findings.ts`:

```ts
interface FinanceFinding {
  id: string;
  agent: "reconciliation";
  at: string;                       // ISO, Australia/Brisbane
  severity: "critical" | "warning" | "info";
  isPeopleFlag: false;              // recon never produces COI/people signals
  entityCode: string;               // "SC" | "CQ"
  domain: string;                   // "bank" | "intercompany" | "journal"
  detector: string;                 // evidenceRef.kind if set, else bank-rec / intercompany-mismatch / journal-posting
  title: string;
  body: string;                     // = Exception.detail
  explanation: string | null;       // Claude one-liner
  evidence: Record<string, unknown>; // { runId, runAt, domain, ...evidenceRef }
  amount: number | null;
  suggestedAction: "freeze" | "notify-tony" | "review" | "approve" | "monitor";
  resolved: boolean;
}
```

Suggested-action mapping: critical → `notify-tony`, warning → `review`, else `monitor`.

Detector codes seen in `evidenceRef.kind`:
- `unposted-manual-journal`
- `late-posted-journals`
- `large-posted-manual-journal`
- (bank/intercompany detectors don't set `kind` — fall back to `bank-rec` / `intercompany-mismatch`)

Query params: `?since=<iso>` and `?include_resolved=1`. Default returns unresolved only, max 500, ordered by severity desc then createdAt desc.

Underlying Prisma `Exception` row (`prisma/schema.prisma:139`) carries `domain, severity, title, detail, amount Decimal(14,2), aiExplanation, evidenceRef Json, dedupKey, resolved, resolvedBy, resolvedAt, createdAt`.

---

## 4. Cron cadence

One job, once daily.

- `cron/railway.toml`: `cronSchedule = "0 21 * * *"` UTC = 07:00 AEST.
- Cron container POSTs `https://<recon>/api/cron/run` with `Authorization: Bearer ${CRON_SECRET}` via `cron/ping.sh` (max-time 290s).
- `app/api/cron/run/route.ts` wraps `runReconciliation()` in a try/catch and calls `sendHeartbeatFailure(error)` on any throw — "silence is not success".
- `runtime = "nodejs"`, `maxDuration = 300`.

All three detectors run inside that single tick. No per-detector schedules.

---

## 5. Write paths

The CLAUDE.md spec says read-only on Xero, but the code has evolved — `lib/recon/write.ts` exists and there are two write surfaces, both gated and audited:

1. **`writeBankTransaction`** — fires when Nicole accepts a `CodingSuggestion` via `/api/coding/[id]/accept`. Creates a Xero BankTransaction in either SC or CQ.
2. **`writeDraftManualJournal`** — Tony 2026-05-27 addition. Hard-locked to `Status: DRAFT`. Used by `/api/journals/draft` and `/api/journals/payroll-draft` (payroll MJ builder). Never posts.

Every write logs to `XeroWriteLog { entityId, triggeredBy, operation, requestPayload, xeroResponse, xeroResultId, status, errorMessage }`. `RECON_MOCK=true` short-circuits the actual Xero call but still logs a mock entry.

Other write-ish API routes (all user-driven, not detector-driven): `/api/matches/[id]/{accept,reject,correct}`, `/api/coding/[id]/{accept,reject}`, `/api/admin/bank-accounts/[id]/toggle`, `/api/imports/[batchId]/delete`, `/api/admin/wipe`, `/api/exceptions/open`, `/api/rules`, `/api/rules/[id]/deactivate`.

**Port implication for jbc-reconciliation:** if Tony wants the Hermes skill to stay strictly read-only / detector-only, the write surfaces and their UI are out of scope and stay in a separate "coding workbench" surface. The skill is just detection + finding emission.

---

## 6. Quirks / gotchas

- **`enabledForCoding` gate everywhere.** Overdraft, low-cash, balance-unavailable, stale-unreconciled, matching, coding — all skip accounts where `BankAccount.enabledForCoding=false`. Cash snapshots and the email cash table still capture all accounts. Without this gate, dormant cards/trust accounts pollute criticals.
- **Credit cards skip overdraft.** `BankAccountType == "CREDITCARD"` is naturally negative — overdraft check is bypassed even when enabled.
- **Intercompany sign convention is normalised.** `abs(|sc| - |cq|) > tolerance` — handles both "equal and opposite" and same-sign conventions.
- **Cross-run dedup is mandatory.** `evidenceRef.uniqueKey` → `Exception.dedupKey` → existing unresolved row found = skip insert and skip Claude (just refresh title/detail/amount/evidenceRef on the original row). Without this, every day re-emits the same draft journals as new. Keys: `unposted-mj:<entity>:<MJID>`, `large-mj:<entity>:<MJID>`, `late-posted-aggregate:<entity>`.
- **Intercompany attached to SC run.** Computed once across both tenants but persisted as exceptions on `scRun.id`. The CQ run won't show intercompany exceptions in its own row.
- **Trial-balance parser is fragile.** `extractAccountFromTrialBalance` matches by account-code prefix in cell text OR `Attributes.Id == "account"|"code"`. Xero report row shapes vary.
- **Lookback window.** `RECON_LOOKBACK_DAYS` (default 90) limits how far back unreconciled bank txns are pulled. Older backlog stays in Xero but doesn't clutter `/coding`. Don't widen blindly — predict cost is per-line.
- **CQ flagged as overdraft risk on seed.** New `BankAccount` rows on CQ tenant are auto-set `isOverdraftRisk=true`. Pure history flag — drives nothing in detector logic today.
- **Heartbeat on failure.** Cron route catches everything and emails `ALERT_RECIPIENTS` via `sendHeartbeatFailure`. Cron is the only place this is called.
- **SMS is per-agent.** Twilio direct from this service to `RECON_SMS_RECIPIENTS`, not via a hub. Fail-quiet — SMS errors are logged but never take the run down. Only `critical` findings SMS.
- **Pull is best-effort.** Each Xero call is try/caught individually inside `pullSnapshot` — a 5xx on Reports doesn't kill the whole snapshot, missing data just becomes a downstream detector exception.
- **`runJournals` uses positive-line sum for amount.** `manualJournalAmount` filters `LineAmount > 0` and sums (not abs). One-sided journals could miss the threshold.
- **Manual journals deep-link is tenant-agnostic.** The Xero URL needs the user already logged in to the right org — no per-tenant slug. Fine for Nicole, confusing in cross-tenant pages.

---

## 7. Required env vars

From `lib/env.ts` (zod-validated, all read centrally):

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres for own state. Required. |
| `XERO_SC_CLIENT_ID` / `XERO_SC_CLIENT_SECRET` / `XERO_SC_TENANT_ID` | SC tenant OAuth + tenant id. |
| `XERO_CQ_CLIENT_ID` / `XERO_CQ_CLIENT_SECRET` / `XERO_CQ_TENANT_ID` | CQ tenant OAuth + tenant id. |
| `ANTHROPIC_API_KEY` | Claude classifier. |
| `ANTHROPIC_MODEL` | Default `claude-sonnet-4-6`. |
| `RECON_UNMATCHED_DAYS` | Business-day age threshold for stale unreconciled GL lines. Default 2. |
| `RECON_JOURNAL_LAG_DAYS` | Business-day post-vs-transaction-date threshold. Default 3. |
| `RECON_LARGE_JOURNAL_AUD` | Large posted-MJ threshold. Default 10000. |
| `RECON_LOOKBACK_DAYS` | How far back to pull unreconciled bank txns. Default 90. |
| `RECON_INTERCOMPANY_TOLERANCE_AUD` | Intercompany mirror-gap tolerance. Default 1. |
| `RECON_LOW_CASH_WARNING_SC_AUD` / `RECON_LOW_CASH_WARNING_CQ_AUD` | Per-entity low-cash warning floor. Optional. |
| `XERO_SC_LOAN_TO_CQ_CODE` / `XERO_CQ_LOAN_FROM_SC_CODE` | Intercompany account codes in each tenant's CoA. |
| `REPORT_RECIPIENTS` | CSV. Daily email goes here. |
| `REPORT_FROM` | From-address for SES. Default `recon@justbettercareqld.com.au`. |
| `ALERT_RECIPIENTS` | CSV. Heartbeat-failure email. |
| `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | SES v2 creds. |
| `CRON_SECRET` | Bearer token gating `/api/cron/run`. Required in prod. |
| `HUB_API_KEY` | Bearer token gating `/api/findings` (Mark's read). Shared across sub-agents. |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | Critical-finding SMS. |
| `RECON_SMS_RECIPIENTS` | CSV of E.164 numbers for critical SMS. |
| `RECON_MOCK` | "true"/"1" swaps Xero + write surfaces for fixtures. |
| `RECON_PUBLIC_URL` | Used to build deep-links in the daily email (`/matches`, `/coding`, dashboard). |
| `NODE_ENV` | dev/test/production. |

---

## 8. Estimated migration difficulty: **Medium**

Why not Small:
- Three detector domains, two-tenant pull, Trial-Balance/BankSummary parsing (fragile), dedup layer, intercompany-attached-to-SC quirk, Claude classifier on every new exception.
- Xero client (`lib/xero.ts`, 800 lines) is non-trivial — OAuth client_credentials, pagination, report walking. Needs a clean port or skill-shared lib.
- Findings shape is already aligned to the Hermes/Mark contract — `lib/findings.ts` is ~ 1:1 copy-pasteable.

Why not Large:
- No real-time anything — pure batch, single cron tick, idempotent.
- Detector logic itself is < 600 lines across `bank.ts + intercompany.ts + journals.ts`.
- All thresholds already in env, no hardcoded config.
- The Stage-2 matching/coding engines, CSV import, payroll-MJ builder, and Xero write surfaces are **out of scope** for a detector-only Hermes skill — leave them in the legacy app, port only `pull → bank/intercompany/journals → persistExceptions → emit-finding`.

Scope-cuts I'd recommend for the port:
- Drop `runMatching`, `runCoding`, `writeBankTransaction`, `writeDraftManualJournal`, CSV import, all `/coding` and `/matches` UI.
- Keep: `pullSnapshot`, `runBankRec`, `runIntercompany`, `runJournals`, `classifyException`, `persistExceptions` (with dedup), `toFinding`, heartbeat-failure, daily-report email, critical SMS.
- Schema for the skill needs only: `Entity, BankAccount, ReconciliationRun, CashSnapshot, Exception, IntercompanyBalance` (six models, ~80 lines of Prisma).

File path: `/Users/anthonysandy/Finance/jbc-hermes-skills/notes/reconciliation.md`
