---
name: jbc-context
description: JBC domain context — entities (SC, CQ), people (approvers, restricted routing), the finance fleet architecture (Mark + 7 specialists as Hermes skills), source systems (Xero API, MYOB CSV/PDF, AlayaCare CSV), Australian compliance (SCHADS, NDIS, SaH, ATO BAS/GST/PAYG/super), and the inviolable guardrails (read-mostly, escalate-never-act, DRAFT-only writes, restricted people-routing). Load this skill whenever a task touches JBC finance.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, context, finance, australia, ndis, sah, schads, xero]
    category: jbc-finance
---

# JBC domain context

Load this skill before any JBC finance task. It defines the language,
roles, guardrails, and architecture every other JBC skill assumes.
Companion skills (`jbc-payables/*`, `jbc-payroll/*`, etc.) trust this is
already in your context — they will NOT re-explain that SC and CQ are
separate taxpayers, or that the people-flag routing exists.

## 1. JBC at a glance

JBC operates two Australian Pty Ltd entities that deliver in-home aged
care + disability services. They are **separate legal taxpayers** —
treat them independently for anything statutory (BAS, GST, PAYG, super,
payroll tax). Consolidated reporting is **management-only**.

| Code | Legal name |
|------|-----------|
| `SC` | Just Better Care Sunshine Coast Pty Ltd |
| `CQ` | Just Better Care Central Queensland Pty Ltd |

Revenue programmes:
- **NDIS** (National Disability Insurance Scheme) — disability funding
- **SaH** (Support at Home) — aged-care home support, replaced HCP
- **Private pay** — direct invoicing

Labour is governed by the **SCHADS Award** (Social, Community, Home Care
and Disability Services Industry Award), which dictates pay-line
penalties, casual loading, public-holiday rules, etc.

## 2. People — who matters and who sees what

| Person | Role | What they see |
|--------|------|---------------|
| Tony Sandy | Director, principal | Everything. The only escalation path for genuinely critical findings. |
| Lindsay | Co-director / approver | People-named findings (HR, controls, restricted). Not individual pay. |
| Nicole | Finance operations | Daily brief, payables draft proposals, AR, individual-pay findings. |
| Christina | Operations / ops manager | Weekly team report (relevant sections). |
| Melissa | Operations | Weekly team report (relevant sections). |
| External accountant | Statutory sign-off | Monthly pack, BAS / tax reports. |

**Restricted routing (non-negotiable):**
- Findings carrying named individuals (HR, conflicts, fraud signals) →
  Tony + Lindsay only.
- Findings carrying individual pay data → Tony + Nicole only.
- Everything else → daily-brief audience (Tony + Nicole).
- Restricted findings are **never** widened. A skill that wants to
  cite a person must set `is_people_flag=true` and accept that
  audience consequence.

## 3. Source systems and how they're consumed

| System | Access mode | Skills that read it | Notes |
|--------|-------------|---------------------|-------|
| Xero (SC + CQ) | OAuth2 API | every finance skill | Two separate tenants. Always fan out per-entity. Env vars `XERO_SC_*` and `XERO_CQ_*`. |
| MYOB | CSV / PDF import (no API) | `jbc-payroll-labour` (the analyser); `create-payroll-journal` writes the journal to **Xero**, not MYOB | MYOB has no API at this stage. |
| AlayaCare | CSV import (no API) | `jbc-revenue-claims`, `jbc-controls-audit` (staff/bank exports) | AlayaCare has no API. |
| Internal Postgres (jbc-findings-db) | direct SQL (psycopg2 etc.) | every skill that emits findings | Schema frozen in `SCHEMA.md` in this repo. |

**Never** invent an API. If a skill wants data and the source has no
API, the skill ingests a CSV/PDF the user supplies.

## 4. The fleet architecture

| Component | Role |
|-----------|------|
| **Mark** (`mark-agent` Next.js app) | Dashboard + chat surface for Tony, Nicole, Lindsay. Reads findings DB. Routes `/qa` chat through the Hermes runtime. |
| **Hermes runtime** (`jbc-hermes` Railway service) | The brain. Loads skills on demand. Runs scheduled detectors via `hermes cron`. Holds durable memory + skills on the `/data/hermes` volume. |
| **Findings DB** (Postgres on `hermes-jbc`, soon `jbc-findings-db`) | Shared `findings` + `audit_runs` tables. Schema in this repo's `SCHEMA.md`. |
| **Honcho** (`honcho-infra` project) | Mark's conversation memory layer. Out of scope for the finance fleet but shared with Adam (Tony's personal agent). |

The 7 finance specialists run **as Hermes skills on this runtime**
(under migration in Phase 2 of the consolidation plan):

```
jbc-reconciliation       jbc-receivables
jbc-payables             jbc-revenue-claims      jbc-controls-audit
jbc-payroll-labour       jbc-tax-compliance
```

Plus two **write** skills (already authored — separate from the
analysers because they cross the read-mostly line):

- `jbc-payables/create-draft-bill` — creates a DRAFT ACCPAY invoice
  in Xero. Hard-locked Status=DRAFT.
- `jbc-payroll/create-payroll-journal` — creates a DRAFT manual
  journal in Xero following the **Craig pattern (journal #673782)**:
  Location-tagged SC + WB, 877 clearing. Hard-locked DRAFT.

## 5. Non-negotiable guardrails

These apply to **every** JBC skill. Inheriting them is not optional.

1. **Read-mostly on source systems.** Default = read-only. The only
   exceptions are the two named DRAFT-write skills above. No skill
   posts, pays, lodges, releases, sends to a third party, or moves
   money. Ever.

2. **Escalate, never act.** Every meaningful decision belongs to a
   named human (Tony, Nicole, Lindsay, or the external accountant).
   Agents prepare drafts and surface exceptions. Humans authorise.

3. **Per-entity isolation.** SC and CQ are separate taxpayers. Every
   detector fans out per tenant. Never compose a "consolidated" find
   for a statutory domain (`tax`, `payroll`).

4. **Restricted routing is enforced at emit time.** When a finding
   names a person or contains individual pay data, set
   `is_people_flag=true`. Don't rely on a downstream filter.

5. **Honesty about provenance.** When citing a number, quote the
   source (Xero invoice ID, CSV file name, audit run ID). Don't
   invent figures.

6. **No statutory consolidation.** Consolidated views are management-
   only. Anything that gets lodged with ATO is per-entity.

7. **Future agents inherit the same rules.** Claire (operations), Tom
   (IT), HR, NDIS-specialist, SaH-specialist — every one runs on this
   runtime, with the same guardrails. Hermes skills are how new
   agents are added; no new Railway projects.

## 6. The findings contract

Every detector emits one or more rows into the `findings` table on
the JBC findings DB. Shape, allowed values, and conventions live in
`SCHEMA.md` in this repo. Read it before writing any new finding.

Key invariants from the schema:
- `entity_code` is `SC`, `CQ`, or `consolidated` (last is forbidden
  for statutory domains).
- `severity` is `critical`, `warning`, or `info`. Picking honestly
  matters — inflated severity erodes Mark's signal.
- `is_people_flag` defaults `false`; set `true` whenever a named
  individual or individual pay is involved.
- `dedupKey` in `evidence` is how skills recognise "I emitted this
  yesterday". Skills MUST set it on every finding they expect to
  re-emit.

## 7. When you're unsure

- Conflicting figures from two skills → flag the conflict, don't
  silently pick a winner.
- A source system is down / rate-limited → emit an `ingest`-domain
  finding, don't silently skip.
- Severity ambiguous → err on the lower severity. Mark prefers
  under-stated to over-stated.
- A write seems necessary but you're not on the write skills above
  → STOP. Either it's a DRAFT proposal a human will action, or it
  doesn't happen.
