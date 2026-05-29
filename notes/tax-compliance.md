# tax-compliance — porting notes for `jbc-tax-compliance`

Source: /Users/anthonysandy/Finance/tax-compliance-agent
Target Hermes skill: jbc-tax-compliance (specialist #6 in migration order, #7 of 7
in original build). No existing stub yet.

Stance: **strictly read-only specialist**. Prepares figures; **never** lodges
with the ATO or any state revenue office. Never posts journals, never edits a
Xero or MYOB transaction, never gives tax advice. Mark/Tony lodge via humans.

Per-entity isolation is statutory: SC and CQ are separate Pty Ltds with separate
ABNs and separate BAS. Findings are always scoped to one entity (or "GROUPED"
for QLD payroll-tax grouping only). Never roll SC+CQ together for statutory
output. Management roll-ups are out of scope for this skill.

---

## 1. Detectors (code → plain English)

All live under `lib/tax/detectors/*` and are registered in `detectors/index.ts`.

Phase 1 — rulesets
- `rules-not-loaded` — no in-force `TaxRuleSet` for ATO or QLD for the current
  period. Loud, runs first; without it every other number is suspect.

Phase 2 — GST (Domain A)
- `gst-position` — live net GST per entity for the open BAS period (info-level
  inventory; emits the running number).
- `gst-cash-shortfall` — cash set aside in the GST-liability account is below
  threshold vs the net GST owed. Warning at 80% coverage, critical at 50%
  (`TAX_GST_CASH_WARNING_PCT` / `CRITICAL_PCT`).
- `gst-treatment-variance` — revenue lines where coded GST status looks
  inconsistent with the supply (NDIS/SaH GST-free vs taxable). Flags for human
  check; does not auto-correct. Calibrated against the external accountant's
  view of JBC's main revenue lines.

Phase 3 — BAS (Domain B)
- `bas-deadline` — BAS due date approaching. Critical inside
  `TAX_DUE_DATE_CRITICAL_DAYS` (7), warning inside `WARNING_DAYS` (30).
- `bas-ready` — period closed and a full `BasPreparation` is assembled with
  every label traceable; status `ready-for-review`. Suggested action
  `review-bas-draft`. Agent never sets `lodged-by-human`.

Phase 4 — PAYG + payroll tax (Domain C)
- `payg-recon` — PAYG withholding from MYOB payroll vs W2 vs GL clearing
  account. Flags variance and/or cash gap (`fund-payg`).
- `payroll-tax` — QLD payroll tax position vs threshold/rate from the QLD
  ruleset. Honours `PAYROLL_TAX_SC_CQ_GROUPED` — when grouped, entityCode is
  `"GROUPED"` and threshold applies once across SC+CQ wages.

Phase 5 — Super + ATO recon (Domains D, E)
- `super-due` — quarterly SG liability accrued vs recorded; flags upcoming
  quarter due dates ahead of time (late super is non-deductible + SGC).
- `ato-recon` — books vs ATO Integrated Client Account where data is
  available; variance above `ATO_RECON_DISCREPANCY_THRESHOLD_AUD` (default 500)
  fires. Suggested action `escalate-to-accountant`.

Phase 6 — Calendar (Domain F)
- `calendar-events` — forward `TaxCalendarItem` rows for BAS, IAS, super,
  payroll tax, income tax per entity. Drives `calendar-alert` findings and the
  recommended tax provision number.

---

## 2. Source systems read

All read-only.

| Source | What is pulled | Notes |
|--------|----------------|-------|
| Xero SC tenant | GST on sales/purchases, GL, GST/PAYG/super clearing accounts | OAuth2, scopes: `accounting.transactions.read`, `journals.read`, `reports.read`, `settings.read`, `contacts.read` |
| Xero CQ tenant | Same as SC, separate tenant | Pulled independently; never consolidated |
| MYOB Advanced | PAYG withholding, gross wages, super | Access method **unconfirmed (CONFIRM C2)** — detectors no-op + flag the gap until creds wired |
| Internal Postgres (shared) | Revenue/Payables/Payroll figures from the other JBC specialists | Cross-agent reads via shared DB |
| ATO Integrated Client Account | Recon target | Where data is available — manual import path likely |

Mock mode: `TAX_MOCK=true` swaps Xero/MYOB for fixtures (`lib/tax/mock/fixtures.ts`).
Useful for previewing reports/findings without real creds.

---

## 3. Findings shape

Conforms to the standard `FinanceFinding` contract (see `lib/findings.ts`).

Fields:
- `id` — stable Exception row id, dedup key for Mark.
- `agent` — always `"tax-compliance"`.
- `at` — ISO-8601 with Brisbane offset.
- `severity` — `critical | warning | info`.
- `isPeopleFlag` — rare for this skill (individual super shortfalls). Restricted
  routing when true; never voiced.
- `entityCode` — `"SC"`, `"CQ"`, or `"GROUPED"` (payroll-tax only). Required.
- `domain` — `gst | bas | payg | payroll-tax | super | ato-recon | calendar | provision | rulesets`.
- `detector` — stable code from §1.
- `title` / `body` — deterministic narrative + amounts.
- `explanation` — plain-English from Claude (AI used only for narrative, never
  for the tax maths).
- `evidence` — BAS period, ruleset version, Xero report ids, etc.
- `amount` — AUD when meaningful (net GST, payroll tax, super).
- `suggestedAction` — bounded vocab: `review-bas-draft`, `investigate-gst-variance`,
  `fund-gst`, `fund-payg`, `fund-super`, `fund-payroll-tax`,
  `escalate-to-accountant`, `calendar-alert`, `notify-tony`, `review`, `monitor`.
- `resolved` — dashboard sets true; do not re-surface.

Severity rules (from spec §6):
- Critical — due date < 7 days with return not prepared; cash shortfall vs
  liability; ATO recon variance over threshold.
- Warning — due date < 30 days; super quarter approaching; GST treatment looks
  inconsistent; provision below recommended.
- Info — live positions, BAS ready, calendar updates.

---

## 4. Cron cadence

- Source agent uses **one** Railway cron tick at `0 21 * * *` UTC (07:00 AEST)
  that POSTs `/api/cron/run` with `Authorization: Bearer $CRON_SECRET`. Single
  endpoint runs the full detector sweep + AI pass + reports.
- For Hermes: a **single daily** schedule is correct — the GST position is
  meant to be a "daily number on a dashboard", not quarterly. Detectors
  themselves are period-aware (BAS quarter ends, super quarter ends) and fire
  the right severity as deadlines approach.
- Optional second tick (intra-day) is fine but not required. BAS quarter ends
  (Mar/Jun/Sep/Dec) are not a separate schedule — `bas-ready` and
  `bas-deadline` detectors handle them on the daily tick.

Run timeout: 300s (full pull + detectors + AI). Heartbeat alert on failure.

---

## 5. Write paths

- **Zero writes to Xero. Zero writes to MYOB. Zero writes to ATO/QRO.**
- Writes only to internal Postgres (append-only):
  - `Exception` (shared model, `sourceAgent="tax-compliance"`)
  - `TaxRuleSet`, `GstPosition`, `BasPreparation`, `PayrollTaxPosition`,
    `SuperObligation`, `TaxCalendarItem`
- `BasPreparation.status` is set by the agent to `draft` → `ready-for-review`.
  Only a **human** ever flips it to `lodged-by-human`.
- Outbound: SES email (3 report classes), Twilio SMS on critical, Hermes
  finding push (`GET /api/findings` gated by `HUB_API_KEY`).

---

## 6. Quirks / gotchas

- **Per-entity isolation is hard.** SC and CQ never consolidate for statutory
  output. Every detector iterates per entity; the only exception is
  `"GROUPED"` entityCode on `payroll-tax` when SC+CQ are grouped for QLD.
- **GST treatment is not uniform.** NDIS and aged-care supports are *mostly*
  GST-free but not all. The detector **uses coded GST status per transaction**
  and flags inconsistency — it must not assume "all care revenue is GST-free".
  Needs CONFIRM with external accountant on main revenue lines.
- **Cash vs accrual.** BAS cycle and method per entity matters
  (`BAS_CYCLE` + per-entity overrides `BAS_CYCLE_SC` / `BAS_CYCLE_CQ`). Cash
  method means GST recognised on payment, not invoice — affects the live
  position and snapshot timing.
- **Rates are versioned, never hardcoded.** GST rate, super guarantee rate,
  payroll tax rate/threshold, PAYG withholding scales live in `TaxRuleSet`
  rows keyed by `effectiveFrom`/`effectiveTo`. A stale rate makes the whole
  return wrong. `rules-not-loaded` detector exists precisely to scream when
  the active period has no ruleset.
- **Payroll tax grouping** changes everything. If `PAYROLL_TAX_SC_CQ_GROUPED`
  is true, threshold applies once across combined wages and findings use
  `entityCode="GROUPED"`. Get it wrong = wrong tax.
- **Chart-of-accounts mapping** for cash-set-aside is per-entity Xero account
  codes (`XERO_GST_LIABILITY_ACCOUNT_CODES`, `_PAYG_WITHHOLDING_`, `_SUPER_CLEARING_`).
  Xero `SystemAccount="GST"` control account is auto-detected; the rest
  require config (CONFIRM C7).
- **ATO recon source is fragile.** ICA data is typically a manual export.
  Treat absence of ICA data as "skip with note", not as "zero variance".
- **AI is for explanation only.** Tax maths is deterministic code. Do not let
  the LLM compute amounts; it only drafts narrative + plain-English
  explanations on findings.
- **Auditability.** Every `BasPreparation` stores `rulesetVersion` + full
  `dataSnapshot` (immutable). Re-runs over the same period must be idempotent.

---

## 7. Required env vars

Core:
- `DATABASE_URL`
- `CRON_SECRET`, `HUB_API_KEY`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`

Xero (both tenants, read-only scopes):
- `XERO_SC_CLIENT_ID`, `XERO_SC_CLIENT_SECRET`, `XERO_SC_TENANT_ID`
- `XERO_CQ_CLIENT_ID`, `XERO_CQ_CLIENT_SECRET`, `XERO_CQ_TENANT_ID`

MYOB (read-only, CONFIRM C2):
- `MYOB_ACCESS_METHOD`, `MYOB_BASE_URL`, `MYOB_USERNAME`, `MYOB_PASSWORD`,
  `MYOB_COMPANY`, `MYOB_BRANCH`

Tax rules + grouping + cycle:
- `ATO_RULESET_VERSION`, `QLD_RULESET_VERSION`
- `PAYROLL_TAX_SC_CQ_GROUPED`
- `BAS_CYCLE`, `BAS_CYCLE_SC`, `BAS_CYCLE_CQ`

Thresholds:
- `TAX_DUE_DATE_CRITICAL_DAYS=7`, `TAX_DUE_DATE_WARNING_DAYS=30`
- `ATO_RECON_DISCREPANCY_THRESHOLD_AUD=500`
- `TAX_GST_CASH_WARNING_PCT=0.8`, `TAX_GST_CASH_CRITICAL_PCT=0.5`
- `TAX_PROVISION_HORIZON_DAYS=90`

CoA mapping (CONFIRM C7):
- `XERO_GST_LIABILITY_ACCOUNT_CODES`
- `XERO_PAYG_WITHHOLDING_ACCOUNT_CODES`
- `XERO_SUPER_CLEARING_ACCOUNT_CODES`

Delivery / routing:
- `AWS_REGION=ap-southeast-2`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `REPORT_FROM`
- `TAX_DAILY_RECIPIENTS`, `TAX_BAS_PACK_RECIPIENTS`, `TAX_ACCOUNTANT_RECIPIENTS`,
  `TAX_MONTHLY_RECIPIENTS`, `TAX_HEARTBEAT_RECIPIENTS`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `TAX_SMS_RECIPIENTS` (CONFIRM C11)

Misc:
- `BASIC_AUTH_USER` / `_PASS` / `_USERS`, `ADMIN_USERNAMES`
- `TAX_MOCK` (fixture mode)

---

## 8. Migration difficulty: **L (Large)**

Why Large:
- **Two source systems** (Xero × 2 tenants + MYOB Advanced) plus internal
  cross-agent reads. MYOB access method is still unconfirmed in source.
- **Eleven detectors** spanning six domains, each with its own ruleset
  dependency and entity scoping rules.
- **Versioned rulesets** (`TaxRuleSet`) are a first-class data model, not
  config — current values for GST/super/payroll tax/withholding scales must
  be confirmed and seeded before any detector is trustworthy.
- **Statutory correctness bar is high.** Per-entity isolation, payroll-tax
  grouping, cash-vs-accrual, GST treatment edge cases — getting any wrong
  produces wrong returns.
- **Three report classes + dashboard pages** (calendar, BAS, exceptions,
  resolve forms) need either porting or replacement.
- **Three persisted artifact types** beyond Exceptions (`BasPreparation`,
  `GstPosition`, `PayrollTaxPosition`, `SuperObligation`, `TaxCalendarItem`,
  `TaxRuleSet`) — schema work required.
- Mitigant: clean detector registry pattern, deterministic maths, LLM only
  for narrative — port is mechanical once schema + ruleset seed are in
  place. Mock fixtures already exist.
