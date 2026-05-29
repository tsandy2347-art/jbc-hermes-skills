# jbc-hermes-skills

Hermes Agent "tap" repo for JBC's custom finance skills. Installed into a
running Hermes Agent instance via:

```bash
hermes skills tap add tsandy2347-art/jbc-hermes-skills
hermes skills install jbc-context                    # load first
hermes skills install jbc-payables/create-draft-bill
hermes skills install jbc-payroll/create-payroll-journal
```

## Skills

| Category | Skill | What it does |
|----------|-------|--------------|
| `jbc-context` | (root) | JBC domain context — entities (SC, CQ), people + restricted routing, fleet architecture, source-system access modes, guardrails. **Load this first.** |
| `jbc-payables` | `create-draft-bill` | Create a DRAFT supplier bill (AP invoice) in Xero. Hard-locked Status=DRAFT. SC or CQ entity. |
| `jbc-payroll` | `create-payroll-journal` | Create a DRAFT manual journal in Xero — Craig pattern #673782 (Location-tagged SC + WB, 877 clearing). Hard-locked DRAFT. |

More land as we migrate the rest of the JBC fleet — see
`../PLAN.md` Phase 2 and the per-specialist briefs under `notes/`.

## Documents in this repo

| File | Purpose |
|------|---------|
| `SCHEMA.md` | The frozen findings/audit-runs DB contract every skill writes to and Mark reads. Additive-only changes. |
| `notes/README.md` | Index + cross-cutting themes from the Phase 0.4 specialist audit. |
| `notes/<specialist>.md` | Per-specialist port brief — detectors, sources, write paths, env vars, gotchas, migration difficulty. |

## Conventions

- Every skill that touches Xero declares the env vars it needs in
  `required_environment_variables` (per the Hermes spec) — Hermes
  auto-passes them to the script sandbox.
- WRITE skills are always hard-locked to DRAFT / no-send / no-post at
  the script layer. Humans post / lodge / send.
- Account codes are NOT pre-validated against a chart-of-accounts —
  Xero validates server-side and rejects unknowns with a clear error.
- MYOB and AlayaCare have NO API at this stage — skills that need
  their data ingest CSV / PDF uploads from the user, not HTTP calls.
- SC and CQ are separate Australian Pty Ltd taxpayers; statutory
  findings are per-entity, never consolidated.
