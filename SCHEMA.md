# Findings Schema — the contract between Hermes skills and Mark

**Last verified against live DB:** 2026-05-29 from `hermes-jbc-production`
Postgres (project `hermes-jbc`, soon renamed `jbc-findings-db` in Phase 4).

This document is the **frozen surface** every JBC finance Hermes skill
writes to and Mark's dashboard reads from. Any change to this schema
must be additive (new optional column / new enum value) — never
breaking. If you find yourself wanting to remove or rename a column,
stop and discuss.

---

## 1. The database

Single shared Postgres on Railway. Hosts every finding produced by the
fleet, plus a few audit/baseline tables that support it.

| Table | Purpose | Writers | Readers |
|-------|---------|---------|---------|
| `findings` | One row per exception emitted by a specialist / skill. | Hermes skills (Phase 2+), legacy specialist services (until cutover). | Mark dashboard, Mark's `/qa` LLM context, any future agent. |
| `audit_runs` | One row per scheduled run of a skill. Counters + duration + failure note. | Same writers as `findings`. | Mark, monitoring. |
| `skills_inventory` | Snapshot of skill files present on the Hermes runtime. | A `hermes-jbc` scanner cron. | Mark's Hermes-activity dashboard. |
| `contact_bank_snapshots` | Vendor bank-detail baseline. Used by controls-audit to detect changes. | Controls-audit only. | Same. |
| `vendor_spend_baselines` | Vendor spend baseline. Same purpose, different metric. | Controls-audit only. | Same. |
| `watched_entities` | Allowlist of legal entities controls-audit is permitted to baseline. | Manually seeded. | Controls-audit only. |

The schema is currently sparse — at time of locking, only `controls-audit`
has written real data (42 findings, 3 audit_runs, two detector codes
`no-abn` and `elevated-user-roster`). Every future skill writes to the
same tables.

---

## 2. `findings` — the row shape (the canonical contract)

Live PostgreSQL DDL, verified 2026-05-29:

```sql
CREATE TABLE findings (
    id              text                       PRIMARY KEY,
    source_agent    text                       NOT NULL,
    run_id          text                       NULL  REFERENCES audit_runs(id),
    detector        text                       NOT NULL,
    domain          text                       NOT NULL,
    severity        text                       NOT NULL,
    entity_code     text                       NOT NULL,
    is_people_flag  boolean                    NOT NULL DEFAULT false,
    title           text                       NOT NULL,
    detail          text                       NOT NULL,
    amount          numeric(14,2)              NULL,
    ai_explanation  text                       NULL,
    evidence        jsonb                      NOT NULL,
    resolved        boolean                    NOT NULL DEFAULT false,
    resolved_by     text                       NULL,
    resolved_at     timestamp with time zone   NULL,
    resolution_note text                       NULL,
    created_at      timestamp with time zone   NOT NULL DEFAULT now()
);

CREATE INDEX findings_open_idx ON findings (source_agent, resolved, severity);
CREATE INDEX findings_run_idx  ON findings (run_id);
```

### TypeScript shape (what Mark consumes via `lib/hermes-findings.ts`)

```ts
interface HermesFinding {
  id: string;
  sourceAgent: string;          // controlled vocab — see §3
  runId: string | null;
  detector: string;             // free text but conventional — see §4
  domain: string;               // free text but conventional — see §5
  severity: "critical" | "warning" | "info";
  entityCode: string;           // "SC" | "CQ" — see §6
  isPeopleFlag: boolean;        // see §7 — affects routing
  title: string;
  detail: string;
  amount: number | null;
  aiExplanation: string | null;
  evidence: Record<string, unknown>;  // jsonb — see §8
  resolved: boolean;
  // resolution metadata not surfaced via Mark today; reserved for future.
  createdAt: Date;
}
```

### Field-by-field

| Field | Required | Allowed values | Meaning |
|-------|----------|----------------|---------|
| `id` | yes | cuid / uuid / any unique text | Stable id. Use the same id when re-emitting an identical finding so Mark dedups. |
| `source_agent` | yes | enum, see §3 | Which skill produced this. |
| `run_id` | no | FK → `audit_runs.id` | Optional but recommended — every find should link to the run that produced it. |
| `detector` | yes | conventional, see §4 | The specific check that fired. |
| `domain` | yes | conventional, see §5 | High-level area (`bank`, `ap`, etc.). |
| `severity` | yes | `critical \| warning \| info` | See §9 for what each level commits Mark to. |
| `entity_code` | yes | enum, see §6 | Which Australian legal entity this concerns. |
| `is_people_flag` | yes | bool, default false | True = restricted routing applies. See §7. |
| `title` | yes | < 120 chars | One-line headline shown on Mark's dashboard. |
| `detail` | yes | plain prose, 1-3 paragraphs | What happened. Mark renders this as-is. |
| `amount` | no | NUMERIC(14,2) — i.e. up to 99,999,999,999.99 AUD | When the finding has a monetary impact. Always AUD. |
| `ai_explanation` | no | prose | Optional secondary text from an LLM pass; lower-trust than `detail`. |
| `evidence` | yes | jsonb, see §8 | Machine-readable references — invoice id, account code, run timestamps, etc. |
| `resolved` | yes | bool, default false | Mark sets to true when a human closes the finding. Skills MUST default to false. |
| `resolved_by`, `resolved_at`, `resolution_note` | no | metadata | Populated by Mark when resolution happens. Skills never touch these. |
| `created_at` | yes | timestamptz | DB default `now()`. Skills should not override. |

---

## 3. `source_agent` — controlled vocabulary

| Value | Source | Notes |
|-------|--------|-------|
| `reconciliation` | jbc-reconciliation skill (Phase 2 #1) | Replaces the legacy `reconciliation-agent` Railway service. |
| `payables` | jbc-payables skill (Phase 2 #2) | Replaces `payables-agent`. |
| `payroll-labour` | jbc-payroll-labour skill (Phase 2 #3) | Replaces `payroll-labour-agent`. The Craig pattern write skill (`create-payroll-journal`) does NOT emit findings. |
| `receivables` | jbc-receivables skill (Phase 2 #4) | Replaces `receivables-agent`. |
| `revenue-claims` | jbc-revenue-claims skill (Phase 2 #5) | Replaces `revenue-claims-agent`. |
| `tax-compliance` | jbc-tax-compliance skill (Phase 2 #6) | Replaces `tax-compliance-agent`. |
| `controls-audit` | jbc-controls-audit skill (Phase 2 #7) | Replaces `controls-audit-agent`. Already in the live data — 42 rows. |

During Phase 2 dual-run windows the OLD specialist may continue to
write under the same `source_agent` value; Mark's dashboard does not
distinguish — and we don't need it to, because we are not yet in
production. Once a new skill is live and its old specialist
decommissioned, the value identifies the new skill from then on.

---

## 4. `detector` — conventions (free text, but disciplined)

Hyphen-cased noun phrase describing the specific check. Examples already
in production:

- `no-abn`
- `elevated-user-roster`

Recommended namespaces from the source audit (Phase 0.4) — these become
the detector vocab once each skill is ported:

```
reconciliation:   bank-overdraft, bank-low-cash, balance-unavailable,
                  stale-unreconciled, intercompany-mismatch,
                  unposted-manual-journal, late-posted-journal,
                  large-posted-journal
payables:         low-extraction-confidence, instruction-like-text,
                  no-abn, invalid-abn, gst-inconsistent,
                  duplicate-invoice, new-supplier-quarantine,
                  new-supplier-bank-detail, low-coding-confidence,
                  ingest-failure, xero-draft-failed, approval-pending,
                  supplier-quarantined-post-draft, payment-run-proposed
payroll-labour:   schads-rate-mismatch, casual-loading-missing,
                  dominant-penalty-error, pay-line-irregular,
                  pre-aggregation-anomaly, mirus-parity-break
receivables:      ar-aging-bucket-shift, debtor-no-contact-30d, ...
                  (full list per receivables.md)
revenue-claims:   unclaimed-revenue, ndis-price-mismatch,
                  sah-cap-breach, budget-approaching, budget-exhausted,
                  service-against-no-plan, plan-expired, ...
tax-compliance:   gst-coding-anomaly, bas-variance, payg-vs-payroll,
                  super-shortfall, payroll-tax-threshold, ...
controls-audit:   no-abn, elevated-user-roster, sod-violation,
                  related-party-flag, bank-detail-change,
                  vendor-master-change, ...
```

**Rule:** if you add a new detector code, also append it to the relevant
specialist notes file under `~/Finance/jbc-hermes-skills/notes/`. The
notes are the cross-reference Mark uses for end-user explanations.

---

## 5. `domain` — conventions

High-level grouping for dashboards. Use one of:

```
bank         intercompany  journal       ap            ar
revenue      payroll       tax           controls      ingest
```

`ingest` is reserved for findings about the data pipeline itself
(e.g. "AlayaCare CSV missing for date X" or "Xero rate-limited"). Skills
that can fail to ingest should emit an `ingest` finding rather than
silently skipping.

---

## 6. `entity_code` — controlled vocabulary

| Value | Legal name | Xero env-var prefix |
|-------|-----------|----------------------|
| `SC` | Just Better Care Sunshine Coast Pty Ltd | `XERO_SC_*` |
| `CQ` | Just Better Care Central Queensland Pty Ltd | `XERO_CQ_*` |
| `consolidated` | Not a legal entity — management-only roll-up | n/a |

`consolidated` is only valid on findings that genuinely cross both
entities (e.g. intercompany mismatch). For statutory-flavour domains
(`tax`, GST coding), `consolidated` is **forbidden** — SC and CQ are
separate Australian taxpayers and a "consolidated GST finding" is
meaningless.

---

## 7. `is_people_flag` — restricted routing

Set to `true` when the finding identifies a named human or contains
individual pay data. The flag affects WHO receives the finding:

- `false`: standard daily brief audience (Tony, Nicole).
- `true`: restricted brief only (Tony, Lindsay for people matters;
  Tony, Nicole for individual pay matters).

**Enforcement is at TWO layers**, both must be respected by every skill:

1. **At finding-emit time.** Set the flag honestly. If unsure, set
   `true` (fail safe).
2. **At delivery time.** Mark (and any future email/SMS dispatcher)
   filters on this flag before composing recipient lists. Skills MUST
   NOT bypass this by leaking restricted content into a non-restricted
   finding's `title` or `detail`.

The current legacy controls-audit-agent enforces this inconsistently
(some at emit, some at send). The Hermes-skill ports should
standardise on **emit-time** so the rule is honoured even by future
consumers that don't go through Mark.

---

## 8. `evidence` — jsonb conventions

Free-form, but with these reserved keys when applicable:

| Key | Type | Meaning |
|-----|------|---------|
| `runAt` | ISO timestamp | When the run that emitted this started. |
| `runId` | string | Mirror of `run_id` column (sometimes useful inline). |
| `kind` | string | Sub-detector code when one `detector` value has variants (e.g. `unposted-manual-journal`). |
| `xeroInvoiceId` | string | Xero `Invoices.InvoiceID`. |
| `xeroContactId` | string | Xero `Contacts.ContactID`. |
| `xeroAccountCode` | string | Account code on the Xero CoA. |
| `dedupKey` | string | Stable key skill uses to recognise "I emitted this yesterday". Skills that dedup write this and check it on next run. |
| `csvSource` | string | When the data came from a CSV ingest, the file name/sha. |
| `period` | string | ISO period (`2026-Q3`, `2026-05`) when finding is period-bound. |

Anything else may be added freely — Mark renders unknown keys as a
key-value table at the bottom of the finding view.

---

## 9. `severity` — what each level commits Mark to

| Level | What it means | What Mark does with it |
|-------|---------------|-----------------------|
| `critical` | Money or compliance at risk today. | Top of daily brief. SMS to Tony if also `entity_code` is operational. Suggested action defaults to `notify-tony`. |
| `warning` | Worth a human's attention this week. | Daily brief, ranked. Suggested action `review`. |
| `info` | Background context — trend, near-threshold, "you might want to know". | Weekly report only by default. Suggested action `monitor`. |

Skills MUST pick honestly. Inflating severity to get attention degrades
the dashboard.

---

## 10. `audit_runs` — the run row

Every scheduled skill run inserts one row before it starts work, then
updates counters + status at the end.

```sql
CREATE TABLE audit_runs (
    id                 text                       PRIMARY KEY,
    source_agent       text                       NOT NULL,
    run_at             timestamp with time zone   NOT NULL DEFAULT now(),
    status             text                       NOT NULL,
    exceptions_count   integer                    NOT NULL DEFAULT 0,
    critical_count     integer                    NOT NULL DEFAULT 0,
    people_flags_count integer                    NOT NULL DEFAULT 0,
    duration_ms        integer                    NULL,
    failure_note       text                       NULL
);
```

`status` is one of: `ok`, `exceptions`, `failed`, `stale` (the last is
set by Mark — not a skill — when a run hasn't happened inside
`MARK_SPECIALIST_STALE_HOURS`).

Rule: **insert the run row at start, update it at end.** A skill that
crashes mid-run leaves a row with no end-state, which Mark surfaces as
"failed: no completion" — the right behaviour, because a silent skill
is a blind spot.

---

## 11. Adding a column safely

If a future skill genuinely needs a new column:

1. Add it `NULL` with a default that doesn't break existing readers.
2. Update this SCHEMA.md in the same commit.
3. Update Mark's `lib/hermes-findings.ts` type to surface the column
   (or leave it server-side-only if not user-facing).
4. Never rename or drop a column. If a column becomes obsolete, leave
   it and stop writing — Mark already tolerates nulls.

---

## 12. How findings flow today (and tomorrow)

```
Today (Phase 0):
  legacy specialist service  ──HTTP──▶  Mark's /api/findings hub
                                          │
                                          ▼
                                 hermes-jbc Postgres (this DB)
                                          │
                                          ▼
                                 Mark dashboard, Mark /qa LLM

Phase 1 onwards:
  Hermes skill (in jbc-hermes runtime) ──direct PG INSERT──▶  same DB
                                                                │
                                                                ▼
                                                       Mark dashboard, /qa

  (no HTTP hub needed once skills run inside Hermes — they have the
   DATABASE_URL in their env and write directly. Faster, fewer failure
   points, same row shape.)
```
