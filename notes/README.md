# Specialist port notes — index

Per-specialist briefs prepared during Phase 0.4 of the JBC consolidation
plan (see `../../PLAN.md`). Each file is the source of truth for what the
matching Hermes skill (`jbc-<name>`) must implement when Phase 2 ports it.

Migration order = `reconciliation` → `payables` → `payroll-labour` →
`receivables` → `revenue-claims` → `tax-compliance` → `controls-audit`.

| # | Specialist | Notes | Lines | Difficulty | Highlights |
|---|------------|-------|-------|-----------|------------|
| 1 | reconciliation | [reconciliation.md](reconciliation.md) | 202 | **M** | Bank, intercompany, journal-integrity detectors. `enabledForCoding` gate everywhere. Write surfaces (`writeBankTransaction`, `writeDraftManualJournal`) recommended **dropped** in the port — write paths live in the existing Hermes skills, not here. |
| 2 | payables | [payables.md](payables.md) | 240 | **L** | 14 detectors, email-channel ingest. Existing `create-draft-bill` Hermes skill covers the write primitive; ~90% of the specialist (ingestion, validation, quarantine, payment-run batching) is not yet in Hermes. |
| 3 | payroll-labour | [payroll-labour.md](payroll-labour.md) | 363 | **L** | Strictly READ-ONLY (correction to earlier assumption). Craig pattern #673782 manual journal is emitted by the existing `create-payroll-journal` Hermes skill — to **Xero, not MYOB** (MYOB has no API). SCHADS award rules, Mirus parity rule, public-holiday gap. |
| 4 | receivables | [receivables.md](receivables.md) | 183 | **M** | 8 AR detectors, daily 07:00 AEST + Monday weekly. Drafts-only email guardrail. Two-tenant fan-out. |
| 5 | revenue-claims | [revenue-claims.md](revenue-claims.md) | 250 | **L** | 13 detector codes, NDIS + SaH. AlayaCare CSV ingest currently falls back to fixtures even when env set — real CSV reader still pending. `CLAIM_AUTO_SUBMIT_ENABLED` hard kill switch. |
| 6 | tax-compliance | [tax-compliance.md](tax-compliance.md) | 246 | **L** | 11 detectors across GST/BAS/PAYG/payroll-tax/super. Versioned `TaxRuleSet`. Strictly read-only — nothing lodged with ATO. SC + CQ taxpayers are independent (no statutory consolidation). |
| 7 | controls-audit | [controls-audit.md](controls-audit.md) | 274 | **L** | 8 live detectors + 3 stubbed (SoD, related-party, journal-integrity blocked on CONFIRMs). People-flag restricted routing enforced at SEND time, not at finding time — must re-enforce in skill. Biggest port surface. 2 orphan Postgres in Railway — confirm before delete. |

Total: 1,758 lines of notes across 7 files. Each file follows the same
8-section template (detectors / sources / findings / cron / writes /
quirks / env vars / migration difficulty), plus per-specialist extras
like Hermes-stub comparisons where one exists.

## Cross-cutting observations

These are themes that appeared in multiple specialists. Phase 2 should
factor them into a shared layer once, not copy them seven times.

- **Daily cron is uniform** — every specialist runs `0 21 * * *` UTC =
  07:00 AEST. Hermes's own scheduler can replace all 7 with one
  per-skill cron.
- **`FinanceFinding` (a.k.a. `Exception`) shape is the contract** with
  Mark — additive-only across all 7. Lock this in `SCHEMA.md` (Phase
  0.5) before any port starts.
- **`HUB_API_KEY` is shared** across every specialist and Mark. One
  shared bearer; Hermes skills should inherit it from the runtime env.
- **People-flag restricted routing** is enforced inconsistently — some
  specialists gate at finding time, controls-audit gates at send time.
  The Hermes-skill layer should standardise on gate-at-finding-time so
  it's harder to get wrong.
- **Per-entity isolation (SC vs CQ)** is universal. Every detector
  fans out twice — once per Xero tenant. Hermes skill template should
  have entity-loop built in.
- **Baselines / dedup / "did I already flag this yesterday"** is
  hand-rolled in every specialist (and inconsistent). This is exactly
  what Hermes durable memory is for — the port to skills is the
  opportunity to do it once, correctly.
