---
name: jbc-revenue-claims
description: Daily JBC revenue + claims specialist. READ-ONLY against AlayaCare (CSV ingest only — no API) and Xero (SC + CQ tenants). Detects revenue leakage (unclaimed-revenue, claim-window-elapsed), pricing breaches (ndis-price-mismatch, sah-cap-breach), and the compound budget detector (service-against-no-plan, plan-expired, budget-approaching, budget-exhausted). Writes findings + an audit_runs row to the shared JBC findings DB. Replaces the legacy `revenue-claims-agent` Next.js Railway service.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, revenue, claims, finance, alayacare, xero, ndis, sah]
    category: jbc-finance
required_environment_variables:
  - name: JBC_FINDINGS_DATABASE_URL
    prompt: Postgres connection string for the shared JBC findings DB (falls back to HERMES_FINDINGS_DATABASE_URL if unset)
    required_for: writing findings + audit_runs
  - name: ALAYACARE_SERVICE_EXPORT
    prompt: Filesystem path to the latest AlayaCare delivered-services CSV export. If unset/missing, an ingest finding `alayacare-export-missing` is emitted and the run exits cleanly.
    required_for: ingest
  - name: XERO_SC_CLIENT_ID
    prompt: Xero SC tenant client ID
    required_for: SC pricing/leakage cross-check
  - name: XERO_SC_CLIENT_SECRET
    prompt: Xero SC tenant client secret
    required_for: SC pricing/leakage cross-check
  - name: XERO_SC_TENANT_ID
    prompt: Xero SC tenant UUID
    required_for: SC pricing/leakage cross-check
  - name: XERO_CQ_CLIENT_ID
    prompt: Xero CQ tenant client ID
    required_for: CQ pricing/leakage cross-check
  - name: XERO_CQ_CLIENT_SECRET
    prompt: Xero CQ tenant client secret
    required_for: CQ pricing/leakage cross-check
  - name: XERO_CQ_TENANT_ID
    prompt: Xero CQ tenant UUID
    required_for: CQ pricing/leakage cross-check
optional_environment_variables:
  - name: HERMES_FINDINGS_DATABASE_URL
    prompt: Fallback name for the findings DB connection string.
  - name: REVENUE_CLAIM_WINDOW_DAYS
    prompt: "Days a delivered service may remain unclaimed before claim-window-elapsed fires. Default 60."
  - name: REVENUE_BUDGET_WARNING_PCT
    prompt: "Budget % triggering budget-approaching. Default 85."
  - name: REVENUE_LOOKBACK_DAYS
    prompt: "Trailing service window pulled from the CSV. Default 30."
  - name: NDIS_PAPL_VERSION
    prompt: "PAPL price guide version label embedded in evidence. Default '2025-26 v1.1'."
  - name: SAH_PRICING_VERSION
    prompt: "SaH price guide version label embedded in evidence. Default 'SaH 2025-11 v1'."
---

# jbc-revenue-claims — daily detector skill

Assumes the `jbc-context` skill is in scope. READ-ONLY everywhere.

## When this runs

Once a day at 07:00 AEST (`0 21 * * *` UTC) via `hermes cron`. Matches
jbc-reconciliation on purpose — both read the same day's snapshots.

## What it does

1. Loads the AlayaCare delivered-services CSV (`ALAYACARE_SERVICE_EXPORT`).
   - **No API.** CSV drop is the only path. Missing/unset → emits an
     `ingest`/`alayacare-export-missing` finding and exits status=`ok`.
   - Participant names tokenised to `initials-XXXX` via SHA1 suffix. Full
     names are never persisted. Trailing `+` / `*` markers stripped per
     `jbc_alayacare_name_markers`.
2. For each entity (SC, CQ) pulls a Xero AR snapshot (read-only OAuth2
   client_credentials) — outstanding ACCREC invoices used as the
   "claimed/invoiced" set for the leakage cross-check.
3. Runs the detector domains:

```
Domain: revenue (leakage)
  unclaimed-revenue         (warning)  per-participant AlayaCare service
                                       with no matching Xero invoice line
  claim-window-elapsed      (critical) unclaimed and aged past window

Domain: revenue (pricing)
  ndis-price-mismatch       (warning)  Xero invoice line priced above PAPL cap
  sah-cap-breach            (critical) Xero invoice line priced above SaH cap

Domain: revenue (budget)
  service-against-no-plan   (critical) service for a participant with no plan/budget loaded
  plan-expired              (critical) service date after plan end
  budget-approaching        (warning)  utilisation ≥ REVENUE_BUDGET_WARNING_PCT
  budget-exhausted          (critical) utilisation ≥ 100%
```

NB: v0.1.0 ships the detector scaffolding and the leakage cross-check
against Xero invoices. Pricing + budget detectors are wired but degrade
to no-op when their inputs (price catalogue, participant budgets) are
absent — they emit a single `info` finding noting the missing input
rather than silently doing nothing.

## Invocation

```
python3 /data/hermes/skills/jbc-revenue-claims/scripts/run_revenue_claims.py
```

Exit 0 on success (incl. "ok with findings"), non-zero on hard failure.

## Hard rules

1. **Read-only everywhere.** AlayaCare CSV is read-only. Xero scopes are
   read-only. No NDIA submission. No participant outbound contact.
2. **`source_agent = 'revenue-claims'`** for every row.
3. **`is_people_flag = TRUE`** when a participant ref appears in the
   finding (leakage-per-participant, budget detectors). **FALSE** when
   the finding is an aggregate revenue figure with no named participant
   (e.g. catalogue absent, ingest failure).
4. **Per-entity fan-out** for Xero. Leakage/pricing run twice — once per
   tenant.
5. **Dedup via `evidence.dedupKey`**:
   - `unclaimed-revenue:<entity>:<serviceId>`
   - `claim-window-elapsed:<entity>:<serviceId>`
   - `ndis-price-mismatch:<entity>:<invoiceId>:<lineItemId>`
   - `sah-cap-breach:<entity>:<invoiceId>:<lineItemId>`
   - `service-against-no-plan:<entity>:<participantRef>:<serviceDate>`
   - `plan-expired:<entity>:<participantRef>:<planId>`
   - `budget-approaching:<entity>:<participantRef>:<periodKey>`
   - `budget-exhausted:<entity>:<participantRef>:<periodKey>`
   - `alayacare-export-missing:<isoDate>`
6. **`entity_code`** is `SC` or `CQ`.
7. **`severity`** stays in `critical | warning | info`.

## Deliberately skipped (from source agent)

Out of scope for v0.1.0 — preserved here as TODOs:

- **NDIA / Services Australia submission** (Domain E). Hard kill switch
  in source (`CLAIM_AUTO_SUBMIT_ENABLED`) and gated on CONFIRM C1/C2/C6.
  Not ported.
- **Weekly claim emails + daily/monthly intelligence reports** (AWS SES).
  Mark's daily brief handles that side now.
- **Twilio critical SMS** — Mark dispatches.
- **AI narration / `classifyException` Anthropic enrichment**. Skipped
  for v0.1.0; `ai_explanation` left NULL.
- **Side tables** — `RevenueRun`, `RevenueSnapshot`, `PricingRuleSet`,
  `ParticipantBudget`, `ClaimBatch`, `AlertDelivery`. The skill writes
  to `findings` + `audit_runs` only. PricingRuleSet/ParticipantBudget
  become future config inputs — TODO surface via a config table.
- **`/exceptions`, `/claims`, `/leakage` UI** — Mark dashboard owns it.
- **`orphan-claim`, `duplicate-claim`, `markup-leakage`,
  `missing-visit-notes`, `ruleset-superseded`, `claim-rejected`,
  `claim-short-paid`, `batch-ready-for-release`,
  `channel-not-configured`** detectors — scaffolding hooks exist in
  `detectors/` for future addition; not emitted in v0.1.0.

## Files

```
jbc-revenue-claims/
  SKILL.md                            # this file
  scripts/
    run_revenue_claims.py             # orchestrator + DB writer
    alayacare_csv.py                  # CSV ingest, name-marker tokeniser
    xero_revenue.py                   # READ-ONLY Xero AR helper (per entity)
    detectors/
      __init__.py
      leakage.py                      # unclaimed-revenue, claim-window-elapsed
      pricing.py                      # ndis-price-mismatch, sah-cap-breach
      budgets.py                      # compound budget detector (4 codes)
```
