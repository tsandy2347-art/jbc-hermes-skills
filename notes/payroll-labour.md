# Payroll & Labour Agent — Port Notes (Next.js → Hermes `jbc-payroll-labour`)

Source: `/Users/anthonysandy/Finance/payroll-labour-agent` (Next.js + Prisma + Railway).
Spec of record: `payroll-labour-agent/CLAUDE.md`. Existing Hermes stub:
`/Users/anthonysandy/Finance/jbc-hermes-skills/skills/jbc-payroll/create-payroll-journal/SKILL.md` (v0.3.0).

Specialist #3 of 7. Two jobs: **payroll integrity** (line-by-line SCHADS recompute
vs paid) and **labour intelligence** (cost %, utilisation, OT/agency). Validates
and flags; never edits, never pays.

> **IMPORTANT FINDING re the task framing.** The task said this specialist "emits a
> DRAFT-locked MYOB journal following the Craig pattern". That is **not what the
> code does**. Verified across `lib/xero.ts`, `lib/sources/myob.ts`, `lib/run.ts`
> and the detectors: this agent is **read-only on every system** (MYOB, AlayaCare,
> Xero). The Craig-pattern DRAFT manual journal (#673782, SC+WB Location-tagged,
> 877 clearing) lives in **Xero** (not MYOB — MYOB has no API in scope), and is
> emitted by `reconciliation-agent/lib/recon/payroll-journal.ts`,
> `mark-agent/lib/mark/payroll-journal-tool.ts`, and the existing Hermes skill
> `jbc-payroll/create-payroll-journal/SKILL.md`. I treat that journal as a separate
> sibling concern below — included for completeness because the existing Hermes
> stub IS the Craig-pattern emitter and the task asked for a comparison.

---

## 1. Detectors

Registry: `lib/payroll/detectors/index.ts`. Each runs against a `DetectorContext`
({ snapshot, lineChecks }) and returns `DraftException[]`.

| Detector code | Domain | Phase | Plain English / SCHADS check |
|---|---|---|---|
| `pay-line-variance` | award (A) | 3 | Per-line variance vs SCHADS recompute. People-flag. Critical if underpaid ≥ $50, else warning. |
| `unverified-line` | award (A) | 3 | Engine couldn't verify (unknown classification / lineType / missing timestamps). Surface for human review — never silently assume match. |
| `systemic-underpayment` | award/B | 3 | Same `lineType` underpaid across ≥ `SYSTEMIC_MIN_STAFF_AFFECTED` distinct staff in one run. **Critical** — legal exposure (Australian underpayment); same-day human action. NOT a people-flag (pattern not name). |
| `super-miscalc` | integrity (B) | 3 | Employer super not equal to `superRate × gross` per the effective-dated `AwardRuleSet.superRate`. Never hardcoded. |
| `ghost-shift` | integrity (B) | 3 | Employee paid with no matching AlayaCare roster/shift. Cross-checked with Controls & Audit agent. |
| `duplicate-payline` | integrity (B) | 3 | Same employee + same lineType + same amount appearing more than once in the run. |
| `labour-cost-pct` | labour-cost (C) | 4 | Labour cost as % of Xero revenue per entity vs `LABOUR_COST_TARGET_PCT_{SC,CQ}` (default 70). |
| `utilisation-drop` | utilisation (D) | 4 | Billable hours ÷ paid hours below `UTILISATION_FLOOR_PCT` (85). The margin gap = unbillable time. |
| `overtime-spike` | overtime (E) | 4 | OT spend > `OVERTIME_SPEND_ALERT_PCT` of total payroll (default 5%). |
| `broken-shift-trigger` | rostering (F) | 6 | Domain F input — broken-shift allowance triggered by avoidable gap; produces `RosteringFinding` for future Rostering Agent. |

SCHADS specifics enforced by the deterministic engine (`lib/award/engine.ts`),
NOT by the detectors:
- Classification × employment type → hourly rate (casual loading rolled in).
- Dominant penalty multiplier across shift window (5-min slices; weekday
  ordinary / weekday evening window / Sat / Sun / public holiday).
- Allowances: broken shift, sleepover, on-call, first-aid, travel time
  (at ordinary), km rate.
- Minimum engagement (separate min for casual vs perm), applied as floor on
  paid duration before rate.
- Overtime first-band multiplier (second-band split deferred to source line shape).
- Super + leave-accrual computed at RUN level, not per line.

---

## 2. Source systems read

All **read-only**. No write methods exist anywhere in the codebase by design.

- **MYOB Advanced (`lib/sources/myob.ts`)** — STUB. Throws `MyobNotConfiguredError`
  until CONFIRM C1 lands. `MYOB_ACCESS_METHOD` env switches `"api" | "csv"`.
  Until then PAYROLL_MOCK runs the canned snapshot.
- **MYOB Pay Activity [Summary] uploads (`lib/payroll/myob-parser.ts`)** — XLSX
  parser. Parses the row-7 column header (Gross / Pre-tax Ded / Taxable / PAYG /
  After-tax / Deductions / Net Pay / Employee Super / Employer Super), walks
  hierarchy (pay run → entity → department → posting class → employee leaf),
  produces `ParsedRun` + `ParsedEmployee`. Handles MYOB "Central Queenland" typo.
  Excel-serial date decoding for the pay-period filter rows. Converted to
  snapshot `PayRun` via `toSnapshotPayRun()` — six synthetic lines per employee
  (gross, pre-tax-ded, payg, post-tax-ded, ee-super, er-super). No hourly detail
  — hourly-shape detectors (OT, broken-shift) need AlayaCare to be useful.
  > PDF parsing of MYOB exports: **not found** — XLSX only.
- **AlayaCare CSV (`lib/sources/alayacare.ts`)** — STUB.
  `AlayacareNotConfiguredError` until CONFIRM C2 (which of the 13 exports carry
  roster / shift / travel / billable). `ALAYACARE_*_EXPORT` IDs and
  `ALAYACARE_CSV_DIR` drop directory.
- **Xero SC + CQ (`lib/xero.ts`)** — READ-ONLY. Scopes:
  `accounting.reports.read accounting.settings.read`. Used only for the
  **revenue side** of labour-cost-as-% (P&L revenue per entity per period).
  Comment in file: "We do NOT pull bills / payments / journals — that is the
  Controls & Audit agent's job."
- **Internal Postgres (Prisma)** — `AwardRuleSet`, `PayrollRun`,
  `PayRunAnalysis`, `PayLineCheck`, `LabourCostSnapshot`, `RosteringFinding`,
  shared `Exception` (sourceAgent=`payroll-labour`), `IngestBatch`.
- **Anthropic** — used ONLY for: severity confirmation + 1-line explanation per
  draft exception (`classifyException`, batches of 8). Deterministic SCHADS
  maths is never delegated to the LLM. Spec §6 "Known constraint": the previous
  analyser hit a token limit by dumping raw payroll; this rewrite pre-aggregates
  server-side and only sends per-exception drafts.

---

## 3. Findings shape (DraftException)

```ts
{
  detector: string,
  domain: "award" | "integrity" | "labour-cost" | "utilisation" | "overtime" | "rostering",
  severity: "critical" | "warning" | "info",
  entityCode: "SC" | "CQ" | "BOTH",
  isPeopleFlag: boolean,            // true → restricted recipients only
  title: string,
  detail: string,
  amount?: number,
  evidenceRef: Record<string, unknown>,
  aiExplanation?: string,           // added by enrichWithAi
}
```

Persisted to shared `Exception` (sourceAgent=`payroll-labour`). Severity rules
(spec §6): Critical = systemic underpayment, super not paid / materially wrong.
Warning = individual variance, OT spike, utilisation drop, single overpayment,
agency above target. Info = labour % within target, trend, rostering findings,
"all clear".

Critical findings → SMS via Twilio (`sendCriticalSms`). People-flag criticals
use a **non-naming** body ("Payroll: people-restricted critical — see dashboard"),
operational criticals get a one-liner with detector + entity + title + amount.

---

## 4. Cron cadence

- `POST /api/cron/run` — `maxDuration = 300` (5 min). Auth: `Bearer CRON_SECRET`
  from Railway sidecar, OR `Bearer HUB_API_KEY` (Mark forwarding a manual
  trigger, with `x-triggered-by` audit header), OR dev-open.
- Sidecar: `cron/Dockerfile` + `cron/ping.sh` + `cron/railway.toml`. Schedule
  pattern follows reconciliation/payables (07:00 AEST = 21:00 UTC daily); exact
  per-service schedules are set in Railway dashboard and **not found** in repo.
- Single orchestrator `runPayroll()` runs all detectors every tick (no
  multi-kind dispatch like payables). Also runs on-demand per pay run (spec §6
  — ideally before the pay run is finalised).
- Reports gated to Brisbane time inside `runPayroll`:
  - 8a payrun-validation — every run (covers the "0 variances" headline).
  - 8b weekly labour — Mondays.
  - 8c monthly intelligence — 1st of month.

---

## 5. WRITE PATH — *(not in this agent)*

**Verified: the payroll-labour agent has ZERO writes to MYOB / AlayaCare / Xero.**
`lib/xero.ts` literally contains no write methods; `lib/sources/myob.ts` and
`lib/sources/alayacare.ts` are read-only stubs. The DB writes (PayrollRun,
Exception, PayRunAnalysis, etc.) are local persistence only.

The **Craig-pattern DRAFT manual journal** lives in three sibling places — none
of them this agent. Recorded here because the existing Hermes stub IS that
emitter (and the task asked for the comparison):

**Journal target:** Xero Manual Journal (NOT MYOB — MYOB has no API in scope;
MYOB is the *source* of the Pay Activity Summary which the journal is BUILT FROM).

**Hard-locked fields** (`SKILL.md` lines 382-388):
- `Status: "DRAFT"` — hard-coded, no path flips it.
- `LineAmountTypes: "NoTax"`.
- `Date` = pay-period end (or user-set journal date).
- `Narration` = user narration + tag `[DRAFT auto-generated by JBC Hermes <Brisbane> AEST]`, truncated to 2500.
- One Manual Journal **per Xero tenant** (SC tenant holds SC+WB; CQ tenant is CQ-only).

**Accounts (Craig's chart, per Journal #673782):**

| Code | Account | Side | Tracked? |
|---|---|---|---|
| 477 | Wages & Salaries — Direct | DR | Location (SC tenant) |
| 477.4 | Wages — Indirect | DR | Location (SC tenant) |
| 478 | Superannuation — Direct | DR | Location (SC tenant) |
| 478.1 | Superannuation — Indirect | DR | Location (SC tenant) |
| 803 | Wages Payable | CR | UNTRACKED |
| 825 | PAYG Withholdings Payable | CR | UNTRACKED |
| 826 | Superannuation Payable | CR | UNTRACKED |
| 877 | Tracking Transfers (clearing) | both | mixed |

**SC vs WB tagging (Location category on SC tenant):**
- Expense DRs (477/477.4/478/478.1) carry `Location = "Sunshine Coast"` OR `"Wide Bay"`.
- Payable CRs (803/825/826) carry **no tag** — shared clearing.
- `877` bridges:
  - `CR 877 (Location-tagged)` per (location × directness) clears the P&L location side to zero.
  - `DR 877 (untracked)` sums all the payable CRs to balance the balance-sheet side.

**Direct vs Indirect rule (Tony 2026-05-27):** Department `Field` = Direct.
Everything else (Admin / Mgmt / Finance / HR / Rostering / HCP / HCP Admin /
NDIS Disability / NDIS SIL) = Indirect.

**Arithmetic identities the script enforces:**
- `Net = Gross − PreTaxDed − PAYG + AfterTax − PostTaxDed` (matches MYOB row-8 grand totals).
- AfterTax allowances roll into the Wages DR (no separate allowances account in JBC's chart).
- PreTaxDed (salary sacrifice) is the same dollar as part of EmpSuper — counted ONCE on Wages Payable CR per Craig's pattern (gets transferred to 826 when the super clearing-house is paid).
- Wages Payable = Net + PreTaxDed + PostTaxDed. Super Payable holds only EmployerSuper.
- `DR sum == CR sum within 1c` is asserted before POST.

**CQ tenant journal:** no Location tracking, no 877 needed. Just DR expenses
(477/477.4/478/478.1) + CR payables (803/825/826), untagged.

---

## 6. Quirks / award-specific gotchas

- **MYOB has the typo "Central Queenland"** in entity names — parser handles both.
- **MYOB access method unconfirmed (CONFIRM C1):** API vs CSV — until resolved
  the live MYOB pull throws and the agent runs against the canned snapshot via
  `PAYROLL_MOCK=1` or against XLSX uploads via `myob-parser.ts`.
- **AlayaCare exports unconfirmed (CONFIRM C2):** which of 13 carry roster /
  shift / travel / billable. Without these, hourly-shape detectors (OT,
  broken-shift) have nothing to compare against.
- **Award rates are versioned, never hardcoded.** `AwardRuleSet.rules` JSON +
  `superRate` Decimal + `effectiveFrom/effectiveTo`. Spec §2.5: a wrong or stale
  rate makes every line-item check wrong. Super guarantee rate also lives here
  (changes typically each July at Fair Work annual review).
- **Casual loading is rolled into the rate lookup** — any explicit `casual-loading`
  pay line is treated as an unverified "shouldn't be a separate line" signal.
- **Penalty multiplier** is the *dominant* multiplier across the shift (majority
  of minutes by 5-min slices) — same approach Mirus uses, captured for the
  parity comparison in phase 7.
- **Minimum engagement** is applied per employment type (casual vs perm),
  flooring `units` before recomputing.
- **Public holidays need a calendar source** — currently dispatched only when
  the source line is explicitly typed `penalty-public-holiday`. Engine doesn't
  detect public holidays itself.
- **Individual pay data is restricted (spec §2.4):** People-flag exceptions go
  only to `PAYROLL_PEOPLE_FLAG_RECIPIENTS` (Tony / Nicole / Lindsay).
  Aggregate-only reports (8b weekly, 8c monthly) go to `LABOUR_REPORT_RECIPIENTS`
  / `LABOUR_MONTHLY_RECIPIENTS`. Routing enforced at send time.
- **The agent never certifies compliance** — flags only. Underpayment in
  Australia carries serious legal exposure; the agent assists the obligation,
  it does not absorb it.
- **Mirus parity & retirement (Phase 7):** Mirus is used only as a payroll award
  interpreter (confirmed). SCHADS engine fully replaces Mirus once parity is
  proven for a full set of pay cycles. Run in parallel, don't switch Mirus off
  before parity. No other agent picks up any Mirus scope.
- **Pre-aggregation rule:** the LLM never sees raw payroll (token-limit lesson
  from the previous analyser). Only per-exception drafts go to
  `classifyException`. Enforced by construction in `lib/anthropic.ts`.
- **IngestBatch consumption** is marked AFTER PayrollRun persists, so a mid-run
  crash leaves the batch consumable (better to double-process than silently
  drop).
- **Two entities, distinct Pty Ltds** — never mix tax data. SC = Just Better
  Care Sunshine Coast Pty Ltd, CQ = Just Better Care Central Queensland Pty Ltd.
  WB (Wide Bay) sits inside SC as a tracking-category line, not a separate
  tenant.

---

## 7. Required env vars

(From `.env.example`.)

Core: `DATABASE_URL`. Auth: `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` or
`BASIC_AUTH_USERS=tony:pwA,...`, `ADMIN_USERNAMES=tony,nicole,lindsay`
(gate people-flag pages and gross-margin column).

Xero (read-only, revenue only): `XERO_SC_CLIENT_ID/SECRET/TENANT_ID`,
`XERO_CQ_CLIENT_ID/SECRET/TENANT_ID`.

MYOB (CONFIRM C1): `MYOB_ACCESS_METHOD=api|csv`, `MYOB_BASE_URL`,
`MYOB_USERNAME`, `MYOB_PASSWORD`, `MYOB_COMPANY_SC`, `MYOB_COMPANY_CQ`,
`MYOB_BRANCH`, `MYOB_CSV_DIR`.

AlayaCare (CONFIRM C2): `ALAYACARE_ROSTER_EXPORT`, `ALAYACARE_SHIFT_EXPORT`,
`ALAYACARE_TRAVEL_EXPORT`, `ALAYACARE_BILLABLE_EXPORT`, `ALAYACARE_CSV_DIR`.

Anthropic: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL=claude-sonnet-4-6`.

Award: `SCHADS_RULESET_VERSION` (unset → latest covering pay period).

Thresholds: `PAY_VARIANCE_TOLERANCE_AUD=1`, `LABOUR_COST_TARGET_PCT_SC=70`,
`LABOUR_COST_TARGET_PCT_CQ=70` (CONFIRM C3), `OVERTIME_SPEND_ALERT_PCT=5`,
`UTILISATION_FLOOR_PCT=85`, `SYSTEMIC_MIN_STAFF_AFFECTED=3`.

Reports / SES: `PAYROLL_PEOPLE_FLAG_RECIPIENTS` (restricted CSV),
`LABOUR_REPORT_RECIPIENTS`, `LABOUR_MONTHLY_RECIPIENTS`,
`PAYROLL_HEARTBEAT_RECIPIENTS`, `REPORT_FROM`, `AWS_REGION=ap-southeast-2`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.

Cron / inbox: `CRON_SECRET`, `HUB_API_KEY`, `ROSTERING_API_KEY` (gate the
outbound `/api/rostering-findings` feed).

SMS: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
`PAYROLL_SMS_RECIPIENTS` (E.164 CSV).

Mock: `PAYROLL_MOCK=1` — bypasses all sources and uses canned snapshot.

---

## 8. Comparison vs existing Hermes skill

Existing: `jbc-hermes-skills/skills/jbc-payroll/create-payroll-journal/SKILL.md`
v0.3.0. **Important:** this stub is a JOURNAL EMITTER, not a payroll analyser.
It and the Next.js payroll-labour agent are **complementary, not overlapping** —
they do different jobs.

| Capability | Next.js payroll-labour | Hermes `create-payroll-journal` stub |
|---|---|---|
| SCHADS line-by-line recompute | ✅ Phase 2-3 (engine + detectors) | ❌ not present |
| Pay-line variance / unverified / systemic underpayment / super / ghost / dup detectors | ✅ | ❌ |
| Labour cost % / utilisation / OT / agency analytics | ✅ Phase 4 | ❌ |
| Rostering findings (Domain F outbound) | ✅ Phase 6 | ❌ |
| AI severity confirm + 1-line explain (pre-aggregated) | ✅ | ❌ |
| MYOB Pay Activity Summary XLSX parsing | ✅ `myob-parser.ts` | ❌ (skill expects user-supplied totals) |
| AlayaCare CSV ingestion | stub | ❌ |
| Xero **revenue read** for labour-% | ✅ read-only | ❌ |
| Xero **DRAFT Manual Journal write** (Craig pattern, 477/477.4/478/478.1/803/825/826/877, SC+WB Location-tagged, hard-locked DRAFT) | ❌ NOT in this agent | ✅ this is the stub's only job |
| Direct/Indirect split (Field=Direct) | not as a write — present only as MYOB department parsed field | ✅ encoded |
| Conversational confirm-before-write (YES gate) | n/a | ✅ |
| Persistent PayLineCheck / Exception / RosteringFinding store | ✅ Prisma | ❌ |
| Cron orchestration / SMS / SES reports | ✅ | ❌ |

Net: the Hermes stub already nails the *write primitive* (DRAFT-locked Craig
journal). The Next.js agent's value — detectors, ingestion, recompute,
analytics, reports — is **entirely unported**. Porting payroll-labour will be a
sibling skill set to the existing journal stub, not a replacement of it.

Suggested Hermes layout post-port:
- `jbc-payroll/create-payroll-journal/` — keep as-is (the write primitive).
- `jbc-payroll-labour/parse-myob-pay-activity/` — XLSX → ParsedRun + ParsedEmployee.
- `jbc-payroll-labour/recompute-schads/` — deterministic engine; pure functions.
- `jbc-payroll-labour/detect-payroll-exceptions/` — runs the 10 detectors over
  a snapshot, returns DraftException[]. No state.
- `jbc-payroll-labour/labour-cost-snapshot/` — pulls Xero revenue + computes %.
- (Persistence + cron + SES + SMS likely stay outside Hermes proper.)

---

## 9. Migration difficulty: **L (Large)**

Why:
- SCHADS engine is the load-bearing piece (CLAUDE.md §4 calls it "the long
  pole") — versioned ruleset, classification × rate × loading, penalty
  multiplier with day-boundary handling, minimum engagement, OT bands,
  allowances. Spec §11 demands line-for-line parity with a known-correct
  historical run before Mirus retires (Phase 7).
- 10 detectors across 6 domains (A–F), some inter-dependent
  (`systemic-underpayment` consumes per-line variance output).
- Persistent state required: `AwardRuleSet` versioning, `PayRunAnalysis` +
  `PayLineCheck` (every line stored), `LabourCostSnapshot` time series,
  `RosteringFinding` outbound contract, shared `Exception`.
- Two source integrations still CONFIRM-blocked (MYOB access method C1,
  AlayaCare export IDs C2) — Phase 1 blocker.
- Strict routing/PII rules (people-flag vs aggregate channels, non-naming SMS
  bodies) need careful Hermes-side enforcement.
- AlayaCare cross-check (ghost-shift) needs roster ingestion before it adds
  value — paired dependency with the future Rostering Agent.

Mitigating:
- Engine is pure functions over `{ruleset, input}` — testable, portable.
- Mock mode (`PAYROLL_MOCK=1`) lets the whole thing run end-to-end without any
  credentials — good Hermes parity-test target.
- Existing Hermes `create-payroll-journal` stub is the ONLY write surface in the
  end-state and is already done well (DRAFT hard-locked, Craig pattern,
  Location tracking discovery, balance assertion). It is the safest piece and
  it's already shipped.

Staging:
- Phase A (M): port the SCHADS engine + MYOB XLSX parser as pure-function skills.
- Phase B (M): port detectors that work off the MYOB summary alone
  (super-miscalc, duplicate-payline, pay-line-variance, systemic-underpayment).
- Phase C (L): MYOB live integration (C1) + AlayaCare ingestion (C2), then
  hourly-shape detectors (ghost-shift, OT, broken-shift) and labour analytics.
- Phase D (M): persistence + cron + reports + SMS — likely outside Hermes proper.
- Phase E (S): Mirus parity verification + retirement.

File written: `/Users/anthonysandy/Finance/jbc-hermes-skills/notes/payroll-labour.md`
