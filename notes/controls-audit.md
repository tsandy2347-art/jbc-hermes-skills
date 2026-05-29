# controls-audit — porting notes (jbc-controls-audit)

Source repo: /Users/anthonysandy/Finance/controls-audit-agent
Migration position: 7 of 7 (last, heaviest). No prior Hermes stub.
Stack: Next.js (App Router) + TypeScript + Prisma/Postgres + Anthropic + Railway cron.
Self-description: "watchdog" — surfaces signals for human review, never verdicts,
never writes to source systems. Two Xero tenants: SC and CQ.

---

## 1. Detectors

Registered in lib/audit/detectors/index.ts. All are pure functions over a
DetectorContext returning DraftException[]. Phase 2 (deterministic) is live;
Phase 3 (SoD) and Phase 4 (related-party) are stubbed pending CONFIRMs.

Live (Phase 2):
- vendor-bank-change   Vendor's bank account on file changed since last snapshot.
                       Critical by default — classic invoice-redirection fraud.
                       Compares current contact bank against ContactBankSnapshot.
- duplicate-invoice    Same vendor, same/near amount, same/near date inside
                       AUDIT_DUPLICATE_WINDOW_DAYS (default 30).
- new-vendor           New supplier added; goes onto WatchedEntity for
                       AUDIT_NEW_VENDOR_WATCH_DAYS (default 90).
- no-abn               Vendor missing ABN or fails format/validity check.
- round-number         Invoice ≥ AUDIT_ROUND_NUMBER_FLAG_AUD (default 2000)
                       that's a round number.
- spend-spike          Vendor spend vs its own learned baseline exceeds
                       AUDIT_VENDOR_SPEND_SPIKE_PCT (default 50%).
- after-hours-activity Payments / journals raised or approved outside
                       AUDIT_AFTER_HOURS_START..END (default 18:30–07:00).
- threshold-gaming     Cluster of payments sitting just under an approval tier
                       (within AUDIT_THRESHOLD_GAMING_BAND_PCT, default 5%).
                       Needs AUDIT_APPROVAL_LIMITS_AUD populated to be useful.

Plus a master-data diff (lib/audit/master-data.ts) that emits both
MasterDataChange rows and additional drafts when a vendor's bank details
change between runs.

Stubbed / planned (PEOPLE-flag-producing):
- Domain A — SoD (Phase 3): created-vendor-and-approved-payment-to-it,
  raised-invoice-and-applied-its-payment, created-and-posted-journal,
  user-with-overbroad-role. Blocked on C4 (real approval limits + authority map).
- Domain B — related-party (Phase 4): vendor-bank == employee-bank;
  shared surname / address / phone / emergency contact between vendor or
  new-hire and existing staff or manager; new hire added by manager whose
  details partially match. Blocked on C2 (MYOB access) + C3 (AlayaCare
  staff/bank export).
- Domain F — journal anomalies: manual journal to rarely-used account,
  round-number journal, period-end journal, per-user journal spike.

Restricted routing (PEOPLE flags): anything in Domain B, or any flag that
names an employee, must have isPeopleFlag = true. Routing matrix enforced
in lib/reports.ts and lib/sms.ts at send-time (not only by convention).

---

## 2. Source systems read

- Xero (read-only) — both tenants SC + CQ. Custom Connection (OAuth2).
  Reads: contacts (vendors + bank details), invoices, payments, journals,
  users, settings, reports. Scopes:
    accounting.transactions.read
    accounting.journals.read
    accounting.reports.read
    accounting.settings.read
    accounting.contacts.read
  Same Xero app credentials as the recon agent — reuse.
- MYOB Advanced (read-only, NOT WIRED). lib/myob.ts is a stub that throws
  MyobNotConfiguredError. MYOB-dependent detectors no-op and emit a single
  `gap` flag per run until C2 lands. Open question: API vs CSV bridge,
  which licence tier, who hosts creds.
- AlayaCare CSV (NOT WIRED). Needed for related-party (Domain B) to see
  staff bank/address/contact data. Neither claire-agent nor payroll-analyser
  currently exports bank fields; AC may need to enable a new export. Blocked.
- Internal Postgres (own DB) — append-only audit store. Read for baselines,
  prior bank snapshots, watch list, prior runs.
- No direct read of "Postgres audit logs" from another system. The orphan
  Railway Postgres instances referenced in PLAN are not consumed here today.

---

## 3. Findings emitted

Two persistence shapes + one external contract.

Internal Prisma models (lib/audit/types.ts + prisma/schema.prisma):
- Exception (SHARED model across all 7 agents; sourceAgent = "controls-audit").
    id, sourceAgent, runId, detector, entityCode ("SC"|"CQ"|"BOTH"),
    domain ("sod"|"related-party"|"vendor"|"payment"|"masterdata"|"journal"),
    severity ("critical"|"warning"|"info"),
    isPeopleFlag (Boolean, default false — RESTRICTED ROUTING TRIGGER),
    title, detail, amount (Decimal?), aiExplanation (Claude-written, neutral),
    evidenceRef (Json — source-system ids), resolved/resolvedBy/resolvedAt,
    resolutionNote, createdAt.
- AuditRun: runAt, status, dataSnapshot (immutable Json), exceptionsCount,
  peopleFlagsCount, criticalCount, durationMs, failureNote.
- WatchedEntity: kind ("vendor"|"employee"|"bankaccount"), reference,
  reason, addedAt, watchUntil, active.
- MasterDataChange: entityCode, recordKind, recordRef, recordLabel, field,
  oldValueMasked, newValueMasked, changedBy, changedAt, detectedAt, reviewed.
- Baseline: scope, reference, metric (Json), updatedAt.
- Plus ContactBankSnapshot, AlertDelivery (heartbeat / SMS dedup).

External contract — FinanceFinding (lib/findings.ts). This is the shape Mark
(the future voice-finance-manager) reads via GET /api/findings, Bearer
HUB_API_KEY. STABLE NAMES, additive only.
    { id, agent: "controls-audit", at (ISO+10:00),
      severity: critical|warning|info, isPeopleFlag, entityCode, domain,
      detector, title, body, explanation, evidence{}, amount,
      suggestedAction: freeze|notify-tony|review|approve|monitor,
      resolved }
suggestedAction rules: vendor-bank-change critical -> "freeze";
severity critical -> "notify-tony"; isPeopleFlag -> "notify-tony";
warning -> "review"; else "monitor".

Severity guidance:
- critical: vendor bank-detail change; vendor/employee shared bank;
  clean SoD breach on a real payment; new finance-system user with broad rights.
- warning:  threshold gaming; surname/address match; duplicate invoice;
  spend spike; after-hours journal.
- info:     new clean vendor; baseline updates; all-clear summaries.

---

## 4. Cron cadence

- Daily 07:00 AEST (= 21:00 UTC) via Railway cron sidecar in cron/.
  cron/railway.toml: cronSchedule = "0 21 * * *". Restart policy NEVER.
  Pings /api/cron/run with Bearer CRON_SECRET.
- Aligned with recon-agent so cross-agent reads see the same day's snapshot.
- Weekly digest fires inside the same daily run when Brisbane weekday == Monday.
- People brief sent only when there is something — no empty email.
- Heartbeat failure email fires on any run-level exception. "Silent watchdog
  = broken watchdog" — explicit design rule.

In Hermes: run as a cron skill once daily at 07:00 Brisbane. Single entry
point ≈ `runAudit({ triggeredBy, sendReports })` in lib/run.ts. Idempotent
per snapshot (each invocation creates its own AuditRun row).

---

## 5. Write paths (confirm read-only)

- Xero: READ-ONLY. lib/xero.ts has no write methods. Scopes are all
  `.read`. Verified in spec §2.1 and README guardrails.
- MYOB: READ-ONLY (when wired). Same posture as Xero.
- AlayaCare: CSV ingest only.
- Internal Postgres: WRITE — this is the agent's own audit store.
  Append-only by design. C7 spec: prod DB role should have SELECT/INSERT
  and a narrow UPDATE on Exception.{resolved,resolvedBy,resolvedAt,
  resolutionNote} + WatchedEntity.active only. No DELETE anywhere.
  Today this is enforced by convention only (PENDING).
- Outbound side-effects: AWS SES email (3 reports + heartbeat) and Twilio
  SMS on critical findings. Both fail-quiet; never block the run.

External read API: GET /api/findings (Bearer HUB_API_KEY) — this is what
Mark / Hermes consumes. The Hermes skill mostly replaces the cron caller
and adds a way to ingest findings for routing.

---

## 6. Quirks / gotchas

- Restricted routing for people flags. isPeopleFlag = true MUST never appear
  in the daily ops brief, ops SMS, weekly digest text, or voice surface.
  People SMS body is non-naming: "people/COI signal warrants review. See
  dashboard (restricted)." Treat this as an assertion at send time, not a
  filter convention — the skill must re-enforce it.
- Signals not verdicts. AI explanation layer is prompted to use neutral
  language ("warrants review", not "evidence of"). Domain B gets extra
  prompt-side enforcement. Any Hermes-side rewrite must preserve this.
- Append-only is the security model. Don't add UPDATE/DELETE in the port.
- Baseline drift. Baselines are loaded then "topped up" with each run's
  computed metrics (lib/run.ts ~L63-66): stored.set(k, v) for every key in
  the new snapshot. This means a single anomalous day can silently shift
  the baseline. Note for migration: consider EMA or hold-out window. Do
  NOT change behaviour silently mid-port — flag it.
- Per-detector errors are caught and emitted as a `warning` flag tagged
  domain="vendor", which is misleading. Carry the bug forward only if the
  port is meant to be byte-equivalent; otherwise tag domain="meta".
- Mock mode: AUDIT_MOCK=true bypasses Xero/MYOB and uses a canned snapshot.
  Useful for porting + report preview before real creds wire up.
- Two orphan Postgres instances in Railway (per PLAN). Neither is THIS
  agent's prod DB — controls-audit gets its own DATABASE_URL. Confirm
  before deleting orphans; one of them may have been an earlier audit DB.
- Watch list dedup uses (entityCode, kind, reference) composite key.
  "BOTH" entityCode is normalised to "SC" on upsert — minor asymmetry.
- Weekly digest only fires inside daily run on Monday Brisbane time. If a
  Monday run fails or is skipped, the weekly is lost. No make-up run.
- Stubbed MYOB + AC paths mean Phase 3 and Phase 4 detectors literally do
  nothing yet. The biggest "surface area" claim is about the SPEC, not the
  currently-shipping code. Useful to scope migration realistically.
- AI scoring throttle: 8 in parallel via batches; no rate-limit handler if
  Anthropic returns 429. Port-side opportunity.

---

## 7. Required env vars

DATABASE_URL                         # own Postgres, append-only

# Basic auth (dashboard + read APIs)
BASIC_AUTH_USER / BASIC_AUTH_PASS    # or BASIC_AUTH_USERS=u:p,u:p
ADMIN_USERNAMES=tony,lindsay         # who sees people-flag pages

# Xero — both tenants, read scopes only
XERO_SC_CLIENT_ID / XERO_SC_CLIENT_SECRET / XERO_SC_TENANT_ID
XERO_CQ_CLIENT_ID / XERO_CQ_CLIENT_SECRET / XERO_CQ_TENANT_ID

# MYOB Advanced — pending C2
MYOB_ACCESS_METHOD / MYOB_BASE_URL / MYOB_USERNAME / MYOB_PASSWORD
MYOB_COMPANY / MYOB_BRANCH

# Anthropic
ANTHROPIC_API_KEY
ANTHROPIC_MODEL=claude-sonnet-4-6

# Detector thresholds
AUDIT_NEW_VENDOR_WATCH_DAYS=90
AUDIT_DUPLICATE_WINDOW_DAYS=30
AUDIT_ROUND_NUMBER_FLAG_AUD=2000
AUDIT_VENDOR_SPEND_SPIKE_PCT=50
AUDIT_THRESHOLD_GAMING_BAND_PCT=5
AUDIT_APPROVAL_LIMITS_AUD=           # PENDING C4
AUDIT_AFTER_HOURS_START=18:30
AUDIT_AFTER_HOURS_END=07:00
AUDIT_BASELINE_LOOKBACK_MONTHS=12

# Routing
AUDIT_OPS_FLAG_RECIPIENTS=tony@,nicole@
AUDIT_PEOPLE_FLAG_RECIPIENTS=tony@,lindsay@   # RESTRICTED
AUDIT_WEEKLY_DIGEST_RECIPIENTS=tony@
AUDIT_HEARTBEAT_RECIPIENTS=tony@
REPORT_FROM=audit@justbettercareqld.com.au

# Delivery
AWS_REGION=ap-southeast-2 / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
AUDIT_SMS_RECIPIENTS=+614...

# Auth tokens
CRON_SECRET                          # gate /api/cron/run
HUB_API_KEY                          # gate GET /api/findings (Mark reads via this)

# Misc
AUDIT_MOCK=                          # bypass Xero/MYOB with canned snapshot

---

## 8. Migration difficulty: L (Large)

Why L:
- Biggest surface area of the 7 specialists: 8 detectors live + 4 detector
  classes (SoD, related-party, journal, people-routing) speced but not built.
- Restricted-routing constraint must be re-enforced in the Hermes skill —
  it's a correctness/safety property, not a stylistic one. Adds testing burden.
- Own Postgres with append-only model + Prisma schema (Exception is the
  cross-agent shared shape — must stay in sync with recon-agent and the
  future Mark consumer).
- Two source-system bridges still PENDING (MYOB C2, AlayaCare C3) — the
  port should match the stub-and-emit-gap pattern, not silently skip.
- Three report channels (ops daily, people restricted, weekly) + SMS +
  heartbeat = five delivery paths to recreate or wrap.
- Baseline behaviour has a known drift quirk that should be discussed
  during port, not silently re-implemented.
- Outbound contract to Mark (FinanceFinding) is stable and consumed by
  another agent — breaking changes here cascade.

Mitigations that drop it toward M:
- Mock mode (AUDIT_MOCK) makes report-flow porting cheap.
- Phase 3 + 4 detectors are currently no-ops; port-now / detect-later is fine.
- Xero credentials are already shared with recon-agent, so OAuth plumbing
  is already understood in the Hermes stack.
