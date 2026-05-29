---
name: jbc-controls-audit
description: Daily JBC controls-audit specialist. READ-ONLY against Xero (SC + CQ tenants). Surfaces governance / fraud-signal exceptions across vendor master-data (no-ABN, vendor-master-change), bank-detail changes on contacts (classic invoice-redirection signal), and the elevated-user roster of each Xero org. Three further detector groups (SoD, related-party, journal anomalies) are stubbed pending upstream data sources. Writes findings + an audit_runs row to the shared JBC findings DB. Replaces the legacy `controls-audit-agent` Next.js Railway service. Invoked by `hermes cron` once daily at 07:00 AEST.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, controls-audit, finance, xero, fraud-signals, governance, restricted-routing]
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
  - name: AUDIT_VENDOR_UPDATED_LOOKBACK_DAYS
    prompt: "Look-back window (days) for considering a vendor master-data record 'recently changed'. Default 2."
  - name: AUDIT_NO_ABN_WARNING_AUD
    prompt: "Spend threshold above which a no-ABN finding is warning rather than info (rolled-up in aggregate). Default 5000."
---

# jbc-controls-audit — daily controls + governance skill

Assumes `jbc-context` in scope: entities SC and CQ, restricted routing,
read-mostly, escalate-never-act. This skill **never** writes to Xero.

## When this runs

Once a day at 07:00 AEST (`0 21 * * *` UTC) via `hermes cron`. Aligned
with `jbc-reconciliation` so the day's snapshot is consistent across
specialists.

## What it does

For each entity (SC and CQ) it pulls a Xero snapshot (Users, Contacts,
ManualJournals — all read-only) and runs three live detector groups
plus three stubbed groups whose namespaces are reserved:

```
Live (Phase 2):
  contacts:
    no-abn                       (warning/info — AGGREGATE, systemic)
    vendor-master-change         (warning — per-vendor, SYSTEMIC, name in evidence)
  bank:
    bank-detail-change           (critical/warning — per-vendor, PEOPLE-FLAGGED,
                                  name masked in title, full name in evidence)
  users:
    elevated-user-roster         (info — AGGREGATE per entity, systemic)

Stubbed (namespace reserved, returns []):
  sod:
    sod-violation                (Phase 3 — needs approval-limits + authority map)
  related-party:
    related-party-flag           (Phase 4 — needs MYOB + AlayaCare staff/bank export)
  journals:
    journal-integrity-named      (Phase 4 — depends on user roster + journal author)
```

Every run inserts an `audit_runs` row at start, writes 0..N rows into
`findings` (dedupKey-driven UPSERT, replaces the legacy baseline tables),
and finalises the run row at end. Schema: `~/Finance/jbc-hermes-skills/SCHEMA.md`.

## Invocation

```
python3 /data/hermes/skills/jbc-controls-audit/scripts/run_controls_audit.py
```

Exit code 0 on success (incl. "ok with exceptions"), non-zero on hard
failure. A crashed mid-run skill leaves the `audit_runs` row in `running`
state, which Mark surfaces as "failed: no completion" — by design.

## Install on the runtime

```
hermes skills install jbc-controls-audit
hermes cron create --schedule "0 21 * * *" --skill jbc-controls-audit \
  --command "python3 /data/hermes/skills/jbc-controls-audit/scripts/run_controls_audit.py"
```

## Hard rules

1. **Read-only on Xero.** Scopes: `accounting.contacts.read`,
   `accounting.settings.read`, `accounting.transactions.read`,
   `accounting.journals.read`. No write scopes ever.
2. **`source_agent = 'controls-audit'`** for every row. Replaces the
   legacy `controls-audit-agent` rows under the same source_agent value —
   dedupKey UPSERT naturally absorbs the 42 historical findings.
3. **Restricted routing enforced at EMIT time.** Findings that name a
   single individual / vendor must:
     - set `is_people_flag = true`
     - mask the name in `title` (initials-XXXX form)
     - put the full name in `evidence.individualName`
   Mark's downstream filter routes on `is_people_flag`. We honour the
   rule at emit so no future consumer can leak by mistake.
4. **Per-entity fan-out** where applicable. Aggregates (no-abn,
   elevated-user-roster) fan out one summary finding per entity.
5. **Dedup via `evidence.dedupKey`.** No separate baseline tables.
   Key conventions:
     `no-abn-aggregate:<entity>:<isoDate>`               (daily rollup)
     `vendor-master-change:<entity>:<contactId>:<fingerprint>`
     `bank-detail-change:<entity>:<contactId>:<bankFingerprint>`
     `elevated-user-roster:<entity>:<isoDate>`           (daily rollup)
   The `<fingerprint>` is a sha256 prefix of the canonical
   master-data tuple (or the bank-account string for bank changes).
   When the underlying value changes, fingerprint changes, a new
   finding is emitted; the prior open finding stays in the DB until a
   human resolves it. This replaces `ContactBankSnapshot` baseline.
6. **Severity vocabulary stays in `critical | warning | info`.**
7. **`entity_code`** is `SC` or `CQ`. No `consolidated` here.

## People-flag policy (this skill)

| Detector                | is_people_flag | Why |
|-------------------------|----------------|-----|
| no-abn                  | false          | aggregate count of vendors, no single named person |
| vendor-master-change    | false          | systemic master-data drift; vendor name is a legal entity, recorded in `evidence` only |
| bank-detail-change      | **true**       | identifies a specific named vendor receiving payment — restricted by default |
| elevated-user-roster    | false          | roster summary (count + masked initials list), not a single named individual |
| sod-violation           | **true**       | (stub) will name internal users when wired |
| related-party-flag      | **true**       | (stub) names employees/vendors when wired |
| journal-integrity-named | **true**       | (stub) names the journal author |

When `is_people_flag = true`, the emit-time invariant is:
- `title` contains the masked form `<initials>-XXXX` (e.g. `JS-XXXX`)
- `evidence.individualName` contains the full name
- `evidence.isRestricted = true`

## Stubbed detectors

`scripts/detectors/sod.py`, `related_party.py`, `journals.py` exist as
no-op modules returning `[]`. They reserve the namespace and document
the upstream blockers so the file layout doesn't churn when the data
sources land.

## Deliberately not ported

- SMS sender, AWS SES email rendering, report HTML — Mark covers
  delivery now.
- Baseline tables (`ContactBankSnapshot`, `VendorSpendBaseline`,
  `WatchedEntity`) — replaced by `evidence.dedupKey` semantics.
- Anthropic `aiExplanation` enrichment — left `NULL`, add later if
  needed.
- Mock mode — drop; live creds are wired through the runtime.
- Heartbeat email — Mark surfaces `audit_runs` status, which is the
  equivalent signal.

## Files

```
jbc-controls-audit/
  SKILL.md
  scripts/
    run_controls_audit.py       # orchestrator + audit_runs lifecycle + emit-time routing guard
    xero_controls.py            # Users / Contacts / ManualJournals — READ-ONLY OAuth
    detectors/
      __init__.py
      contacts.py               # no-abn (aggregate), vendor-master-change
      bank.py                   # bank-detail-change (people-flagged)
      users.py                  # elevated-user-roster (aggregate)
      sod.py                    # stub
      related_party.py          # stub
      journals.py               # stub
```
