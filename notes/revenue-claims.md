# revenue-claims-agent → jbc-revenue-claims (port notes)

Source: `/Users/anthonysandy/Finance/revenue-claims-agent`
Target skill: `jbc-revenue-claims` (5th in migration order, no existing stub).
Specialist #5 of 7. Owns the revenue side: every AlayaCare-delivered service
must be claimed at the correct price against the correct funding, and the cash
must arrive. Two parallel rule engines: NDIS (PAPL 2025‑26 v1.1) and Support
at Home (SaH, replaced HCP 1 Nov 2025). Two entities: SC (Sunshine Coast),
CQ (Central Queensland).

## 1. Detectors

All registered in `lib/revenue/detectors/index.ts`. Each receives a
`DetectorContext` (services, claims, budgets, rulesets) and returns
`DraftException[]`.

| code | domain | severity | phase | plain English |
|---|---|---|---|---|
| `unclaimed-service` | unclaimed | warning | 2 | AlayaCare visit with no matching `Claim` row. Headline leakage $$ — projects what the claim would have been at the in‑force ruleset price. |
| `orphan-claim` | validation | critical | 2 | `Claim` row that points at a `serviceId` not in the loaded service set (potential false claim — fraud risk). |
| `price-above-cap` | pricing | critical | 3 | Claim's `unitPrice` exceeds the NDIS PAPL / SaH price limit for that item under the ruleset in force on the service date. |
| `price-mismatch` | pricing | warning | 3 | Claim priced UNDER cap (stale rate card costing JBC margin). Tolerance 1¢. |
| `ruleset-superseded` | ruleset | warning | 3 | Draft/held claim still carries an older `rulesetVersion` than the current registry entry — re-construct unless service date predates current ruleset. |
| `duplicate-claim` | validation | critical | 3 | >1 non-cancelled / non-rejected claim against the same `DeliveredService`. Only sneaks in via re‑submission without cancelling the prior row. |
| `budgets` (compound) | budget | critical/warning | 4 | Emits four detectors: `service-against-no-plan`, `plan-expired`, `budget-approaching`, `budget-exhausted`. SaH uses MONTHLY pool window; NDIS uses plan-window. |
| `missing-visit-notes` | evidence | warning | 2 | SaH visit with `visitNotesPresent=false`. SaH evidence-of-visit rule requires notes for every billable visit. |
| `markup-leakage` | markup | warning | 3 | Care-Partner coordination service delivered but billed at base SW rate instead of marked-up CP code. Dollar figure = lost markup, manager-only view. SAH-only (CP markup never rolled into NDIS). |
| `claim-rejected` | outcome | warning | 6 | Claim status=rejected — categorise + resubmit/write-off; never leave silent. |
| `claim-short-paid` | outcome | warning | 6 | Claim status=part-paid with `outcomeAmount` < `totalClaimed` (gap > 1¢). |
| `batch-ready-for-release` | validation | info/warning | 5 | `ClaimBatch` in `held-over-ceiling` state. Escalates to warning after 24h. |
| `channel-not-configured` | channel | info | 1 | `NDIS_CLAIM_CHANNEL` / `SAH_CLAIM_CHANNEL` env unset — blocks Domain E (submission). Fires every run until configured. |

## 2. Source systems read

Three inputs, all READ-ONLY:

1. **AlayaCare — delivered services (CSV export)**.
   - **AlayaCare has no usable API** for delivered-service data — flows in as
     CSV drop. Real path is stubbed pending CONFIRM C3 (CSV drop path vs S3
     bucket vs eventual API).
   - Module: `lib/revenue/alayacare.ts`.
   - Entry: `pullDeliveredServices({periodStart, periodEnd})`. If
     `REVENUE_MOCK=true` OR `ALAYACARE_SERVICE_EXPORT` env unset → returns
     in-tree fixtures from `lib/revenue/fixtures/services.ts`.
   - If `ALAYACARE_SERVICE_EXPORT` is set but the real CSV reader is not yet
     implemented (current state) → console.warn and falls back to fixtures.
     **The real CSV ingest path is a known gap** — needs to be implemented
     during port.
   - Row shape (`AlayaServiceRow`): `externalId`, `entityCode` SC|CQ,
     `participantNameRaw`, `program` NDIS|SAH, `serviceDateISO` (Brisbane
     local), `supportItemRaw`, `hours`, `visitNotesPresent`, `raw` (full row
     kept for audit).
   - **Participant name tokeniser**: `maskParticipantRef()` strips trailing
     `+` (= "true SaH participant" marker per `jbc_alayacare_name_markers`)
     and `*` (= unconfirmed, ignored — NOT discharged), then masks the name
     to `initials-XXXX` (hash-of-name suffix). Full names never stored.
   - **Idempotent persist**: `persistDeliveredServices()` upserts on
     `(sourceSystem='alayacare', externalId)` — safe to re-run. Updates only
     drift fields (`hours`, `visitNotesPresent`, `sourceRef`).
   - Manual import script: `scripts/import-alayacare.ts` (30-day window).

2. **Xero — SC and CQ** (read-only scopes only).
   - Two separate tenants (`XERO_SC_*`, `XERO_CQ_*`).
   - Used to cross-check claimed vs invoiced (recon-agent reads the same
     snapshots at the same 07:00 AEST tick).

3. **Internal DB** (Postgres via Prisma) — `DeliveredService`, `Claim`,
   `ClaimEvent`, `ParticipantBudget`, `PricingRuleSet`, `ClaimBatch`,
   `RevenueSnapshot`, `RevenueRun`, `Exception`, `AlertDelivery`, `Entity`.

(NDIA API Gateway exists in env-template but submission domain E is gated
until CONFIRM C1.)

## 3. Findings emitted

Findings = `Exception` rows in the shared schema (with
`sourceAgent="revenue-claims"`). Same model used by every JBC specialist.

DraftException shape (pre-persist):
```
{
  detector:      string                  // detector code
  domain:        "unclaimed"|"pricing"|"validation"|"budget"|
                 "outcome"|"markup"|"ruleset"|"channel"|"evidence"
  severity:      "critical"|"warning"|"info"
  entityCode:    "SC"|"CQ"|"BOTH"
  program?:      "NDIS"|"SAH"|"BOTH"
  participantRef?: string                // masked
  isPeopleFlag:  boolean                 // rarely true here
  title:         string
  detail:        string
  amount?:       number                  // AUD, optional
  evidenceRef:   Record<string, unknown> // claimId/serviceId/etc
}
```
Persisted row adds `runId`, `aiExplanation` (optional, fail-quiet AI pass).

**AI enrichment**: `lib/anthropic.ts::classifyException()` runs per draft in
batches of 8 — may downgrade/upgrade severity (`confirmedSeverity`) and
attach `aiExplanation`. Skipped silently when `ANTHROPIC_API_KEY` unset.

**Hermes inbox**: `GET /api/findings` (Bearer `HUB_API_KEY`). Findings are
pulled, never pushed (per `feedback_hermes_architecture`). SMS-on-critical
is per-agent direct via Twilio (no hub).

## 4. Cron cadence

- Schedule: `0 21 * * *` UTC = **07:00 AEST daily** (`cron/railway.toml`).
- Matches recon-agent on purpose — both read the same day's snapshots.
- Trigger endpoint: `POST /api/cron/run` (auth: `Authorization: Bearer
  ${CRON_SECRET}`).
- Default run period: trailing 30 days, Brisbane local.
- Conditional reports inside the run:
  - daily ops brief — every run
  - weekly digest — Mondays Brisbane
  - budget-exhaustion — when any budget flag fired in last 26h
  - monthly intelligence — day 1 Brisbane
- Failures persist a `RevenueRun(status="failed", failureNote=...)` and email
  `REVENUE_HEARTBEAT_RECIPIENTS`. Never throws to the caller.

## 5. Write paths

**Confirmed read-only on external systems.** Hard guardrails:

- **AlayaCare**: read-only (CSV/API import only — never edits).
- **Xero (SC + CQ)**: read-only scopes.
- **NDIA / Services Australia**: never submits a real claim unless
  `CLAIM_AUTO_SUBMIT_ENABLED=true` (hard kill switch — default OFF) AND
  batch under `CLAIM_AUTO_SUBMIT_BATCH_CEILING_AUD`. Anything else held in
  `ClaimBatch.status="held-over-ceiling"` for explicit human release click
  on `/claims`.
- **Participants**: zero outbound contact, ever. `lib/email.ts` hard-guards
  against personal-domain destinations.

Internal writes only: own Postgres (RevenueRun, Exception, DeliveredService,
Claim, ClaimEvent, ClaimBatch, RevenueSnapshot, AlertDelivery). Outbound
notifications: AWS SES (email) + Twilio (critical SMS).

## 6. Quirks / gotchas

- **AlayaCare name markers** (`jbc_alayacare_name_markers`):
  - trailing `+` → true SaH participant (set `trueSahParticipant=true`)
  - trailing `*` → unconfirmed, **does NOT mean discharged** — strip and
    ignore
  - both markers stripped before participant ref is hashed.
- **SaH pool resets monthly** (`feedback_sah_pool_monthly`). Budget
  detector uses calendar-month windows for SaH vs plan-window for NDIS —
  separate code paths. Do not unify.
- **HCP is fully wound down** (`jbc_hcp_finished`). No HCP statement, no
  Package Management Fee, no Third Party Service + Visit Premium. Don't
  port any HCP code if encountered.
- **Care Partners are SAH-only** (`feedback_jbc_cps_sah_only`). CP markup
  leakage detector must never roll up NDIS into CP totals.
- **Profit/margin visibility gated** (`feedback_profit_visibility`).
  Markup/margin columns on `/leakage` only visible when basic-auth
  username ∈ `MANAGER_USERNAMES`. Skill needs to preserve this gating if
  it surfaces leakage data anywhere user-visible.
- **PricingRuleSet is effective-dated, never hardcoded**. Claim priced
  using ruleset in force on the **service date** (not the run date).
  `ctx.rulesets.inForce(program, serviceDate)`.
- **Idempotent service import** keyed on `(sourceSystem, externalId)`.
  Update only drift fields (hours, visitNotesPresent, sourceRef).
- **Period vs full‑plan budget**: SaH spent computed from current calendar
  month services; NDIS spent uses `claimedToDate` on the budget row.
- **Plan management mix matters** (CONFIRM C5): agency-managed → claiming;
  plan-managed + self-managed → receivables (handed to Receivables Agent).
  This agent only handles agency-managed claims.
- **AI enrichment may rewrite severity** — detectors emit a draft severity,
  Anthropic may downgrade/upgrade. Mock mode + missing API key both
  fall through cleanly.
- **No participant emails**, ever. Hard-coded guard in `lib/email.ts`.

## 7. Required env vars

Database / auth:
- `DATABASE_URL`
- `BASIC_AUTH_USER` + `BASIC_AUTH_PASS` OR `BASIC_AUTH_USERS=u1:p1,u2:p2`
- `ADMIN_USERNAMES`, `MANAGER_USERNAMES`
- `CRON_SECRET`, `HUB_API_KEY` (Hermes inbox)

Xero (read-only):
- `XERO_SC_CLIENT_ID`, `XERO_SC_CLIENT_SECRET`, `XERO_SC_TENANT_ID`
- `XERO_CQ_CLIENT_ID`, `XERO_CQ_CLIENT_SECRET`, `XERO_CQ_TENANT_ID`

AlayaCare (CSV ingest):
- `ALAYACARE_SERVICE_EXPORT` — path/URL of CSV export (blank = mock)
- `ALAYACARE_BASE_URL`, `ALAYACARE_API_KEY` (placeholders, not wired)

Claiming channels (CONFIRM C1):
- `NDIS_CLAIM_CHANNEL`, `SAH_CLAIM_CHANNEL` = api|bulk-upload|manual
- `NDIA_API_BASE_URL`, `NDIA_API_CLIENT_ID`, `NDIA_API_CLIENT_SECRET`,
  `NDIA_API_ACCESS`
- `NDIS_PROVIDER_REG_SC`, `NDIS_PROVIDER_REG_CQ`
- `SAH_APPROVAL_REF_SC`, `SAH_APPROVAL_REF_CQ`

Pricing:
- `NDIS_PAPL_VERSION` (default `2025-26 v1.1`)
- `SAH_PRICING_VERSION` (CONFIRM C9)

Submission gating:
- `CLAIM_AUTO_SUBMIT_BATCH_CEILING_AUD` (default 5000)
- `CLAIM_BUDGET_WARNING_PCT` (default 85)
- `CLAIM_AUTO_SUBMIT_ENABLED` (hard kill switch, default OFF)

Reports (AWS SES + Twilio):
- `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `REPORT_FROM`, `REVENUE_DAILY_RECIPIENTS`,
  `REVENUE_WEEKLY_RECIPIENTS`, `PLAN_REVIEW_RECIPIENTS`,
  `REVENUE_MONTHLY_RECIPIENTS`, `REVENUE_HEARTBEAT_RECIPIENTS`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `REVENUE_SMS_RECIPIENTS`

Anthropic + misc:
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`)
- `REVENUE_GOAL_AUD_ANNUAL` (default 3000000 — Tony's $3M anchor)
- `REVENUE_MOCK` (skip all externals, use fixtures)

## 8. Migration difficulty

**L (Large).** This is the heaviest specialist in the JBC system.

Why:
- 13 detector codes (compound budget detector emits 4 sub-codes) across 9
  domains and 7 build phases — substantial port surface vs other agents.
- Two parallel rule engines (NDIS + SaH) with different windowing
  (plan-window vs monthly pool) and a versioned `PricingRuleSet`
  registry — non-trivial to keep effective-dated.
- AlayaCare CSV ingest is **not implemented in source** — falls back to
  fixtures even when `ALAYACARE_SERVICE_EXPORT` is set. The port needs the
  real CSV path built (header mapping, encoding, name-marker tokeniser
  preserved, idempotency on `externalId`).
- Submission domain E + outcome domain F are gated on CONFIRMs C1/C2/C6
  (claiming channel, ceiling, outcome export shape) — still scaffolded.
- Strong cross-system surface: Xero SC + CQ, AlayaCare, NDIA, Twilio SMS,
  SES email, Anthropic enrichment, Hermes findings inbox.
- Rich UI surface (`/`, `/exceptions`, `/exceptions/[id]`, `/claims`,
  `/leakage`) plus manager-only gated columns to reproduce or stub.
- Strict guardrails to preserve verbatim: read-only on AlayaCare/Xero;
  CLAIM_AUTO_SUBMIT_ENABLED default OFF; no participant emails;
  HCP fully removed; CP markup is SAH-only; profit-visibility gated.

Recommended port order inside the skill:
1. CSV-ingest path for AlayaCare (with mock + fixtures preserved).
2. PricingRuleSet registry + `inForce(program, date)` lookup.
3. Phase 2 detectors (unclaimed-service, orphan-claim, missing-visit-notes).
4. Phase 3 detectors (price-checks, duplicate-claim, markup-leakage).
5. Phase 4 budget detector (SaH monthly vs NDIS plan-window).
6. Phase 6 outcome detectors (rejected, short-paid).
7. Phase 5 batch-ready + channel-config (last; gated on CONFIRMs).
8. Reports (daily/weekly/budget/monthly) + SMS criticals + Hermes inbox.
