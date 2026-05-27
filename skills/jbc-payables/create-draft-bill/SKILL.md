---
name: create-draft-bill
description: Create a DRAFT supplier bill (AP invoice) in Xero for SC or CQ. Status is hard-locked to DRAFT — Nicole / Tony / the external accountant clicks Post in Xero. The agent never posts. First JBC payables skill (replaces the Next.js payables-agent quarantine→draft flow).
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [xero, payables, jbc, drafts, ap, finance]
    category: jbc-finance
required_environment_variables:
  - name: XERO_SC_CLIENT_ID
    prompt: Xero SC tenant client ID (Just Better Care Sunshine Coast Pty Ltd)
    required_for: SC bills
  - name: XERO_SC_CLIENT_SECRET
    prompt: Xero SC tenant client secret
    required_for: SC bills
  - name: XERO_SC_TENANT_ID
    prompt: Xero SC tenant UUID
    required_for: SC bills
  - name: XERO_CQ_CLIENT_ID
    prompt: Xero CQ tenant client ID (Just Better Care Central Queensland Pty Ltd)
    required_for: CQ bills
  - name: XERO_CQ_CLIENT_SECRET
    prompt: Xero CQ tenant client secret
    required_for: CQ bills
  - name: XERO_CQ_TENANT_ID
    prompt: Xero CQ tenant UUID
    required_for: CQ bills
---

# Create a DRAFT supplier bill in Xero

## When to use

The user wants to record a supplier bill (AP) in Xero. The bill is created
with `Status: DRAFT` — it shows up in Xero's drafts list awaiting a named
human to review and POST. The agent never posts.

Use this skill when the user says any of:

- "Draft a bill for $X from <supplier>"
- "Create a draft AP entry"
- "Stick this invoice in Xero as a draft"
- "Record the supplier bill for review"

DON'T use this skill for:

- **Posting** (Status=AUTHORISED). Drafts only. If the user explicitly asks
  to post, decline and offer the draft path.
- **Sales invoices** (Type=ACCREC). This skill is AP only.
- **Manual journals**. Different skill (or recon agent's `/journals/draft`
  for now).
- **Payments**. Drafts don't get paid; humans handle payment from posted
  invoices in Xero.

## Hard rules

1. **Status is hard-locked to DRAFT** inside `scripts/create_draft_bill.py`.
   There is no argument or flag that flips it to POSTED. Do not attempt to
   "post" via this skill — it cannot.
2. **Entity is SC or CQ** (Just Better Care Sunshine Coast Pty Ltd vs Central
   Queensland Pty Ltd). They are separate taxpayers — never mix lines from
   one into a bill for the other.
3. **At least one line, every line has** a positive amount + an account
   code. Xero validates account codes server-side — if a code doesn't exist
   Xero rejects with a clear error which you relay to the user.
4. **Reference** (the human-readable journal/invoice number from the supplier)
   should be quoted verbatim from the source document when the user provides
   it. Otherwise leave it blank — Xero auto-generates an InvoiceNumber.

## Procedure

1. Confirm with the user:
   - **entity** (`SC` or `CQ`)
   - **supplier name** (e.g. "Telstra") — or a Xero ContactID if known
   - **lines**: each with `amount` (AUD, positive), `account_code` (Xero
     code), optional `description`
   - **date** (defaults to today, Brisbane)
   - **reference** (supplier's invoice number, optional)

2. Account codes:
   - If the user provided codes, TRUST them. Xero's server-side validation
     is the gate. Do not pre-validate; do not ask the user to "check Xero
     first".
   - If inferring, pick from common AP chart conventions (5xxx COGS,
     6xxx operating expenses, 1xxx assets). NOTE in your proposal that
     you're inferring and the human should adjust before YES if they
     prefer different codes.

3. Propose the draft inline (entity, supplier, date, line table, total).
   End with: "Reply YES to confirm and I'll create the draft in Xero now."

4. ON EXPLICIT YES from the user — and only then — invoke the script:

   ```bash
   python3 scripts/create_draft_bill.py \
     --entity SC \
     --supplier 'Telstra' \
     --date 2026-05-27 \
     --reference 'INV-12345' \
     --lines '[{"amount": 500.00, "account_code": "6010", "description": "Mobile plans April"}]'
   ```

5. On success the script prints JSON with `InvoiceID`, `InvoiceNumber`,
   `Total`, and a `xero_link` deep-link. Quote those back to the user and
   add: "Nicole / Tony / the external accountant clicks Post in Xero when
   ready."

6. On error (script exits non-zero with `ok: false`), quote the Xero
   error message verbatim and ask the user how they'd like to proceed
   (often: pick a different account code, or use a known supplier
   contact id).

## Examples

### A. Single-line bill, user-supplied codes

> User: "Draft a $234.50 bill from Bunnings to SC, code 5200, narration: site supplies"

You propose:
- Entity: SC
- Supplier: Bunnings
- Date: today
- Line 1: $234.50, code 5200 ("site supplies")
- Total: $234.50
- "Reply YES to confirm…"

User: "YES" → invoke script with those args → return link.

### B. Multi-line bill, inferred codes

> User: "Create a draft bill from Vodafone, $1,200 split 80% to CQ field (telecoms), 20% to CQ admin (telecoms)"

You propose:
- Entity: CQ
- Supplier: Vodafone
- Date: today
- Line 1: $960, code 6010 (Telecommunications — Field) ← INFERRED
- Line 2: $240, code 6011 (Telecommunications — Admin) ← INFERRED
- Total: $1,200
- Note: "I've guessed codes 6010/6011 — adjust if your chart uses
  different codes before YES."
- "Reply YES to confirm…"

### C. Bad account code (Xero rejects)

> User provided code: 9999 (doesn't exist)

Script returns: `{"ok": false, "error": "Xero 400: Account code 9999 has not been found"}`

You reply: "Xero rejected the draft — 'Account code 9999 has not been
found'. Pick a real code from your chart and I'll try again."

## Files

- `scripts/create_draft_bill.py` — the actual Xero POST. Uses
  `client_credentials` OAuth (same pattern the existing JBC fleet uses;
  no user-OAuth needed). Hard-codes `Type: ACCPAY` and `Status: DRAFT`.
