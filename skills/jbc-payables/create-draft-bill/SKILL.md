---
name: create-draft-bill
description: Create a DRAFT supplier bill (AP invoice) in Xero for JBC SC or CQ. Status is hard-locked to DRAFT in code — Nicole / Tony / the external accountant clicks Post in Xero. The agent never posts. First JBC payables skill, replaces the Next.js payables-agent quarantine→draft flow.
version: 0.2.0
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
human (Nicole / Tony / the external accountant) to review and POST. The
agent never posts.

Triggers:
- "Draft a bill for $X from <supplier>"
- "Create a draft AP entry"
- "Stick this invoice in Xero as a draft"
- "Record the supplier bill for review"

NOT for:
- Posting (Status=AUTHORISED). Drafts only. If the user explicitly asks
  to post, decline and offer the draft path.
- Sales invoices (Type=ACCREC) — AP only.
- Manual journals (use recon's `/journals/draft` endpoint).
- Payments — drafts don't pay; humans handle payment in Xero.

## Hard rules — these never bend

1. **Status is hard-locked to DRAFT** in the Python below. There is NO
   argument that flips it to POSTED. Do not attempt to "post" via this
   skill — it cannot.
2. **Entity is SC or CQ** (separate Pty Ltd taxpayers). Never mix lines
   across entities in one bill.
3. **At least one line, every line has** a positive amount + an account
   code. Xero validates account codes server-side; if a code doesn't
   exist Xero rejects with a clear error which you relay verbatim to
   the user.
4. **Don't pre-validate account codes** against any chart — Hermes does
   not hold the JBC chart of accounts. Xero is the gate.

## Procedure

1. Confirm with the user, conversationally:
   - **entity** (`SC` or `CQ`)
   - **supplier name** (e.g. "Telstra") — or Xero ContactID if known
   - **lines**: each with `amount` (AUD, positive), `account_code`,
     optional `description`
   - **date** (defaults to today, Brisbane)
   - **reference** (supplier's invoice number, optional)

2. Account codes: if the user provided them, trust them. If inferring,
   pick plausible AP chart codes (5xxx COGS, 6xxx operating expenses)
   and NOTE you're inferring so the user can correct before YES.

3. Propose the draft inline (entity, supplier, date, line table, total).
   End with: **"Reply YES to confirm and I'll create the draft in Xero now."**

4. ON EXPLICIT YES — and only then — invoke the Python below via the
   `execute_code` tool, substituting the user's parameters into
   `PARAMS` at the top. Do NOT call this on an ambiguous reply.

5. Quote the result back to the user — `InvoiceNumber`, `Total`,
   `xero_link` — and add: "Nicole / Tony / the external accountant
   clicks Post in Xero when ready."

6. On error from Xero, relay the error message verbatim and ask how
   the user wants to proceed.

## The script (run via execute_code on YES)

```python
import base64, datetime as _dt, json, os, sys
import urllib.error, urllib.request

# ─── PARAMS — substitute from the user's confirmed proposal ───
PARAMS = {
    "entity": "SC",                   # "SC" or "CQ"
    "supplier_name": "Telstra",       # Xero name lookup
    "supplier_contact_id": None,      # OR a known Xero ContactID UUID
    "date": None,                     # "yyyy-mm-dd" or None = today (Brisbane)
    "reference": None,                # supplier invoice number, optional
    "narration": None,                # appended to reference for traceability
    "lines": [
        {"amount": 1.00, "account_code": "6010", "description": "Smoke test"},
    ],
}
# ──────────────────────────────────────────────────────────────

XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_API = "https://api.xero.com/api.xro/2.0"
WRITE_SCOPES = "accounting.transactions accounting.contacts accounting.settings.read"

def _creds(entity):
    p = entity.upper()
    if p not in ("SC", "CQ"):
        raise ValueError(f"entity must be SC or CQ, got {entity!r}")
    return {
        "client_id": os.environ.get(f"XERO_{p}_CLIENT_ID", ""),
        "client_secret": os.environ.get(f"XERO_{p}_CLIENT_SECRET", ""),
        "tenant_id": os.environ.get(f"XERO_{p}_TENANT_ID", ""),
    }

def _token(creds):
    if not creds["client_id"] or not creds["client_secret"]:
        raise RuntimeError("XERO_*_CLIENT_ID / _CLIENT_SECRET env vars not set")
    basic = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()
    ).decode()
    req = urllib.request.Request(
        XERO_TOKEN_URL,
        data=f"grant_type=client_credentials&scope={WRITE_SCOPES}".encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Xero token exchange failed: {e.code} {e.read().decode()[:300]}") from e

def create_draft_bill(p):
    creds = _creds(p["entity"])
    if not creds["tenant_id"]:
        raise RuntimeError(f"XERO_{p['entity'].upper()}_TENANT_ID not set")
    if not p["lines"]:
        raise ValueError("lines required")

    payload_lines = []
    for i, ln in enumerate(p["lines"]):
        if not isinstance(ln, dict):
            raise ValueError(f"lines[{i}] must be an object")
        if "amount" not in ln or float(ln["amount"]) <= 0:
            raise ValueError(f"lines[{i}].amount must be positive")
        if not str(ln.get("account_code", "")).strip():
            raise ValueError(f"lines[{i}].account_code required")
        payload_lines.append({
            "Description": ln.get("description") or p["supplier_name"],
            "Quantity": ln.get("quantity", 1),
            "UnitAmount": float(ln["amount"]),
            "AccountCode": str(ln["account_code"]).strip(),
            **({"TaxType": ln["tax_type"]} if ln.get("tax_type") else {}),
        })

    brisbane_now = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=10))
    )
    tag = f"[DRAFT auto-generated by JBC Hermes {brisbane_now:%Y-%m-%d %H:%M AEST}]"
    if p.get("narration"):
        tag += f" — {p['narration']}"
    final_ref = f"{p['reference']} {tag}" if p.get("reference") else tag
    final_ref = final_ref[:255]  # Xero hard cap

    contact = {"ContactID": p["supplier_contact_id"]} if p.get("supplier_contact_id") else {"Name": p["supplier_name"]}

    body = {
        "Type": "ACCPAY",
        "Status": "DRAFT",                                 # HARD LOCKED — Tony 2026-05-27
        "Date": p.get("date") or _dt.date.today().isoformat(),
        "Contact": contact,
        "Reference": final_ref,
        "LineItems": payload_lines,
        "LineAmountTypes": "Exclusive",
    }

    token = _token(creds)
    req = urllib.request.Request(
        f"{XERO_API}/Invoices",
        data=json.dumps({"Invoices": [body]}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Xero-Tenant-Id": creds["tenant_id"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:600]
        try:
            j = json.loads(err)
            msg = j.get("Message") or j.get("Detail") or err
        except json.JSONDecodeError:
            msg = err
        raise RuntimeError(f"Xero {e.code}: {msg}") from e

    inv = data["Invoices"][0]
    return {
        "ok": True,
        "entity": p["entity"].upper(),
        "InvoiceID": inv["InvoiceID"],
        "InvoiceNumber": inv.get("InvoiceNumber"),
        "Status": inv.get("Status"),
        "Total": inv.get("Total"),
        "SubTotal": inv.get("SubTotal"),
        "TotalTax": inv.get("TotalTax"),
        "Reference": inv.get("Reference"),
        "Contact": (inv.get("Contact") or {}).get("Name"),
        "xero_link": f"https://go.xero.com/AccountsPayable/Edit.aspx?InvoiceID={inv['InvoiceID']}",
    }

try:
    result = create_draft_bill(PARAMS)
    print(json.dumps(result, indent=2))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
    sys.exit(1)
```

## Examples

### A. Single-line, user-supplied code
> User: "Draft a $234.50 bill from Bunnings to SC, code 5200, narration site supplies"

Propose lines, end with YES prompt. On YES, set PARAMS:
```
entity: "SC", supplier_name: "Bunnings",
lines: [{"amount": 234.50, "account_code": "5200", "description": "site supplies"}]
```
Run the block. Return `xero_link`.

### B. Multi-line, inferred codes (flag uncertainty)
> User: "Bill from Vodafone, $1,200 split 80% CQ field telecoms 20% CQ admin telecoms"

Propose: line 1 $960 code 6010 (inferred), line 2 $240 code 6011 (inferred).
NOTE you guessed codes, ask user to correct or YES.

### C. Bad code, Xero rejects
Script returns `{"ok": false, "error": "Xero 400: Account code 9999 has not been found"}`.
Quote the message, ask user for a real code, retry.
