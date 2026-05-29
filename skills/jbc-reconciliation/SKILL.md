---
name: jbc-reconciliation
description: Daily JBC reconciliation specialist. READ-ONLY against Xero (SC + CQ tenants). Runs 10 detectors across three domains — bank (overdraft, low cash, balance unavailable, stale unreconciled), intercompany (codes not configured, balance unreadable, mismatch), and journal (unposted manual journals, late-posted journals, large posted manual journals). Writes findings + an audit_run row to the shared findings DB. Replaces the legacy `reconciliation-agent` Next.js Railway service. Invoked by `hermes cron` once daily at 07:00 AEST.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [jbc, reconciliation, finance, xero, bank, intercompany, journal]
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
  - name: XERO_SC_LOAN_TO_CQ_CODE
    prompt: Xero account code in SC's CoA representing "Loan to CQ" (intercompany asset)
    required_for: intercompany detector
  - name: XERO_CQ_LOAN_FROM_SC_CODE
    prompt: Xero account code in CQ's CoA representing "Loan from SC" (intercompany liability)
    required_for: intercompany detector
  - name: HUB_API_KEY
    prompt: Reserved for future shared-bearer needs. Not used today — skill writes directly to the findings DB.
    required_for: future
optional_environment_variables:
  - name: HERMES_FINDINGS_DATABASE_URL
    prompt: Fallback name for the findings DB connection string (used when JBC_FINDINGS_DATABASE_URL is unset).
  - name: RECON_UNMATCHED_DAYS
    prompt: "Business-day age threshold for stale unreconciled GL bank lines. Default 2."
  - name: RECON_JOURNAL_LAG_DAYS
    prompt: "Business-day post-vs-transaction-date threshold for late-posted journals. Default 3."
  - name: RECON_LARGE_JOURNAL_AUD
    prompt: "Large posted manual journal threshold (AUD). Default 10000."
  - name: RECON_LOOKBACK_DAYS
    prompt: "How far back to pull unreconciled bank txns (days). Default 90."
  - name: RECON_INTERCOMPANY_TOLERANCE_AUD
    prompt: "Intercompany mirror-gap tolerance (AUD). Default 1."
  - name: RECON_LOW_CASH_WARNING_SC_AUD
    prompt: "SC low-cash warning floor (AUD). Optional — if unset, low-cash warnings are not emitted for SC."
  - name: RECON_LOW_CASH_WARNING_CQ_AUD
    prompt: "CQ low-cash warning floor (AUD). Optional — if unset, low-cash warnings are not emitted for CQ."
---

# jbc-reconciliation — daily detector skill

Assumes the `jbc-context` skill is in scope: entities SC and CQ, restricted
routing, read-mostly, escalate-never-act. This skill never writes to Xero.

## When this runs

Once a day at 07:00 AEST (`0 21 * * *` UTC), triggered by Hermes's own
cron. Install once, then register the cron entry (see Install below).
It can also be invoked manually for ad-hoc verification.

## What it does

For each entity (SC and CQ in parallel) it pulls a Xero snapshot
(read-only OAuth2 client-credentials) and runs three detector domains:

```
Domain A — bank
  bank-overdraft            (critical)
  bank-low-cash             (warning, optional thresholds)
  balance-unavailable       (critical)
  stale-unreconciled        (warning)

Domain B — intercompany  (computed once across BOTH tenants, attached to SC)
  intercompany-codes-not-configured  (critical)
  intercompany-balance-unreadable    (critical)
  intercompany-mismatch              (critical)

Domain C — journal
  unposted-manual-journal       (warning; escalates to critical > 5 business days)
  late-posted-journal           (warning, aggregate w/ top10 in evidence)
  large-posted-journal          (warning, per-journal)
```

Every detector run inserts an `audit_runs` row at start, writes 0..N rows
into `findings` (with dedupKey for idempotency), and updates the audit_runs
row with counters + status at end. Schema: `~/Finance/jbc-hermes-skills/SCHEMA.md`.

## Invocation

```
python3 /data/hermes/skills/jbc-reconciliation/scripts/run_reconciliation.py
```

Exit code 0 on success (including "ok with findings"), non-zero on hard
failure. The run row gets `status='ok' | 'exceptions' | 'failed'`
accordingly — a crashed mid-run skill leaves an `audit_runs` row Mark
surfaces as "failed: no completion", which is the correct behaviour.

## Install on the runtime

```
# From the jbc-hermes container shell:
hermes skills install jbc-reconciliation
hermes cron create --schedule "0 21 * * *" --skill jbc-reconciliation \
  --command "python3 /data/hermes/skills/jbc-reconciliation/scripts/run_reconciliation.py"
```

(The exact `hermes cron create` flags may vary by runtime version — adjust
to whatever the local `hermes cron --help` documents.)

## Hard rules

1. **Read-only on Xero.** No `Status: AUTHORISED`, no writes whatsoever.
   If the task needs a draft manual journal, that is the existing
   `jbc-payroll/create-payroll-journal` skill — NOT this one.
2. **`source_agent = 'reconciliation'`** for every row written.
3. **`is_people_flag = false` always.** Recon never emits people-flag
   findings (per the notes file). If a future check would name a person,
   it belongs in `jbc-controls-audit`, not here.
4. **Per-entity fan-out.** Bank and journal detectors run twice — once
   per tenant. Intercompany runs once, attached to SC.
5. **Dedup via `evidence.dedupKey`.** Re-runs must not duplicate. Key
   convention:
     `bank-overdraft:<entity>:<xeroAccountId>`
     `bank-low-cash:<entity>:<xeroAccountId>`
     `balance-unavailable:<entity>:<xeroAccountId>`
     `stale-unreconciled:<entity>:<xeroAccountId>`
     `intercompany-codes-not-configured`
     `intercompany-balance-unreadable`
     `intercompany-mismatch:<isoDate>`     (date keeps daily history)
     `unposted-mj:<entity>:<ManualJournalID>`
     `late-posted-aggregate:<entity>:<isoDate>`
     `large-mj:<entity>:<ManualJournalID>`
6. **Severity vocabulary stays in `critical | warning | info`.** Don't
   widen.
7. **`entity_code`** is `SC` or `CQ`. Intercompany findings are attached
   to `SC` per the legacy quirk (single source of truth — saves Mark's
   dashboard from showing each mismatch twice).

## PITFALLS (the 11 gotchas — read these)

1. **`enabledForCoding` is no longer enforced inline.** The legacy app
   gated overdraft/low-cash/balance-unavailable/stale-unreconciled on
   `BankAccount.enabledForCoding=true`. The skill currently treats
   *all bank accounts* as in-scope because we no longer carry the
   admin's enable/disable state in the findings DB. **Mitigation:** the
   dedupKey is per-account, so an account flagged today repeats only on
   change; Nicole can resolve once and stay quiet. TODO: surface a
   per-account opt-out via a config table when needed.
2. **Credit cards skip overdraft.** `BankAccountType == "CREDITCARD"`
   is naturally negative — overdraft check is bypassed.
3. **Intercompany sign convention normalised** — `abs(|sc| - |cq|) > tolerance`.
4. **Cross-run dedup is mandatory.** Every finding sets
   `evidence.dedupKey`; the inserter does an "is this key already open?"
   lookup and skips re-insert if so. Replaces the old baseline-storage
   pattern (quirk #11).
5. **Intercompany attached to SC.** One finding, not two.
6. **Trial-balance parser is fragile.** Matches by account-code prefix
   in cell text OR `Attributes.Id == "account"|"code"`. Xero report
   shapes vary.
7. **Lookback window.** `RECON_LOOKBACK_DAYS` (default 90) limits how
   far back unreconciled bank txns are pulled.
8. **`runJournals` uses positive-line sum.** One-sided journals could
   miss the threshold — preserved from source for parity.
9. **Manual journals deep-link is tenant-agnostic.** The Xero URL
   assumes the user is already logged in to the right org.
10. **Pull is best-effort.** Each Xero call is try/caught individually
    — a 5xx on Reports doesn't kill the whole snapshot; missing data
    becomes a downstream detector exception (`balance-unavailable` or
    `intercompany-balance-unreadable`).
11. **Baseline drift.** The legacy agent stored last-seen state on the
    `Exception` row and refreshed in place. The skill replaces that
    with the `dedupKey` lookup. If a finding's title/detail/amount
    needs to change while the original row stays "unresolved", do an
    UPDATE on the existing row (the inserter handles this).

## Deliberately skipped (TODOs from the source agent)

- **`runMatching` / `runCoding`** — Stage-2 prediction engines. Not
  detector-emitting; out of scope for a detector-only skill.
- **`writeBankTransaction` / `writeDraftManualJournal`** — write
  surfaces. Out of scope (see hard rule 1).
- **Claude classifier (`classifyException`)** — the optional LLM pass
  that produced `ai_explanation`. Skipped for v0.1.0; `ai_explanation`
  is left NULL. Add when needed.
- **Daily-report email + critical-finding SMS** — Mark and the
  brief-builder cover this end of the pipeline now.
- **CSV bank-statement uploads + the `/coding` and `/matches` UI** —
  out of scope.
- **`CashSnapshot` / `IntercompanyBalance` history tables** — the
  source agent kept time-series tables for charting. The skill does
  not (yet); the findings + audit_runs tables are enough for Mark.

## Files

```
jbc-reconciliation/
  SKILL.md                       # this file
  scripts/
    run_reconciliation.py        # orchestrator + DB writer
    xero_client.py               # READ-ONLY OAuth + endpoint helpers
    detectors/
      __init__.py
      bank.py                    # Domain A
      intercompany.py            # Domain B
      journal.py                 # Domain C
```
