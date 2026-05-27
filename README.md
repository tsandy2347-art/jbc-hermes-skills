# jbc-hermes-skills

Hermes Agent "tap" repo for JBC's custom finance skills. Installed into a
running Hermes Agent instance via:

```bash
hermes skills tap add tsandy2347-art/jbc-hermes-skills
hermes skills install jbc-payables/create-draft-bill
```

## Skills

| Category | Skill | What it does |
|----------|-------|--------------|
| `jbc-payables` | `create-draft-bill` | Create a DRAFT supplier bill (AP invoice) in Xero. Hard-locked Status=DRAFT. SC or CQ entity. |

More land as we migrate the rest of the JBC fleet (payroll-labour →
receivables → revenue-claims → tax-compliance → controls-audit → mark,
recon last).

## Conventions

- Every skill that touches Xero / MYOB / AlayaCare declares the env
  vars it needs in `required_environment_variables` (per the Hermes
  spec) — Hermes auto-passes them to the script sandbox.
- WRITE skills are always hard-locked to DRAFT / no-send / no-post at
  the script layer. Humans post / lodge / send.
- Account codes are NOT pre-validated against a chart-of-accounts — Xero
  validates server-side and rejects unknowns with a clear error.
