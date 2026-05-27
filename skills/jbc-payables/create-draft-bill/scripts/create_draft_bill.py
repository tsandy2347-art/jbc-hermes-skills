#!/usr/bin/env python3
"""
Create a DRAFT supplier bill (AP invoice) in Xero. Hard-locked status.

Used by the Hermes skill `jbc-payables/create-draft-bill`. Status is
overwritten to "DRAFT" inside this script regardless of caller — there is
NO path here that creates an AUTHORISED (posted) bill. Posting is a
human action in Xero.

Auth: `client_credentials` OAuth — same pattern as the existing JBC fleet
(reconciliation-agent, payables-agent, etc.). Env vars per entity:
  XERO_SC_CLIENT_ID / _SECRET / _TENANT_ID  (Sunshine Coast Pty Ltd)
  XERO_CQ_CLIENT_ID / _SECRET / _TENANT_ID  (Central Queensland Pty Ltd)

Output: JSON object on stdout. {"ok": true, ...} on success, {"ok": false,
"error": "..."} on failure. Exit code 0 on success, 1 on failure.

Usage:
  python3 create_draft_bill.py \\
    --entity SC \\
    --supplier 'Telstra' \\
    --date 2026-05-27 \\
    --reference 'INV-12345' \\
    --lines '[{"amount": 500.00, "account_code": "6010", "description": "Mobile April"}]'
"""

import argparse
import base64
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.request

XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_API = "https://api.xero.com/api.xro/2.0"

# Write scopes already enabled on both JBC Xero apps (the existing fleet
# uses these for BankTransaction writes). accounting.contacts lets us
# create a supplier-name-only contact on the fly when no ContactID given.
WRITE_SCOPES = "accounting.transactions accounting.contacts accounting.settings.read"


def _creds(entity: str) -> dict:
    p = entity.upper()
    if p not in ("SC", "CQ"):
        raise ValueError(f"entity must be SC or CQ, got {entity!r}")
    return {
        "client_id": os.environ.get(f"XERO_{p}_CLIENT_ID", ""),
        "client_secret": os.environ.get(f"XERO_{p}_CLIENT_SECRET", ""),
        "tenant_id": os.environ.get(f"XERO_{p}_TENANT_ID", ""),
    }


def _get_access_token(creds: dict) -> str:
    if not creds["client_id"] or not creds["client_secret"]:
        raise RuntimeError(
            "Xero client_id / client_secret not configured — set the "
            "XERO_<entity>_CLIENT_ID and _CLIENT_SECRET env vars"
        )
    basic = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()
    ).decode()
    body = f"grant_type=client_credentials&scope={WRITE_SCOPES}".encode()
    req = urllib.request.Request(
        XERO_TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["access_token"]
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:400]
        raise RuntimeError(f"Xero token exchange failed: {e.code} {body_text}") from e


def create_draft_bill(
    *,
    entity: str,
    supplier_name: str,
    supplier_contact_id: str | None,
    lines: list,
    narration: str | None,
    bill_date: str | None,
    reference: str | None,
) -> dict:
    creds = _creds(entity)
    if not creds["tenant_id"]:
        raise RuntimeError(f"XERO_{entity.upper()}_TENANT_ID env var not set")

    # Pre-flight validation — fail clean before hitting Xero so the error
    # surface is something the caller can fix without round-tripping.
    if not lines or not isinstance(lines, list):
        raise ValueError("lines must be a non-empty list")
    payload_lines = []
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            raise ValueError(f"lines[{i}] must be an object")
        if "amount" not in ln or float(ln["amount"]) <= 0:
            raise ValueError(f"lines[{i}].amount must be a positive number")
        if "account_code" not in ln or not str(ln["account_code"]).strip():
            raise ValueError(f"lines[{i}].account_code is required")
        item = {
            "Description": ln.get("description") or supplier_name,
            "Quantity": ln.get("quantity", 1),
            "UnitAmount": float(ln["amount"]),
            "AccountCode": str(ln["account_code"]).strip(),
        }
        if ln.get("tax_type"):
            item["TaxType"] = ln["tax_type"]
        payload_lines.append(item)

    token = _get_access_token(creds)

    # Contact lookup: prefer ContactID when supplied (no risk of creating
    # a duplicate). Otherwise pass Name only — Xero will match-or-create.
    contact_block: dict
    if supplier_contact_id:
        contact_block = {"ContactID": supplier_contact_id}
    else:
        contact_block = {"Name": supplier_name}

    # Auto-suffix the reference with a draft tag so reviewers know where
    # this came from. If the user provided a reference, keep it prefix +
    # append the tag. The Hermes-skill provenance helps Nicole spot
    # auto-drafted entries amongst manually-entered ones.
    brisbane_now = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=10))
    )
    draft_tag = f"[DRAFT auto-generated by JBC Hermes {brisbane_now:%Y-%m-%d %H:%M AEST}]"
    if narration and narration.strip():
        draft_tag += f" — {narration.strip()}"
    final_reference = f"{reference} {draft_tag}" if reference else draft_tag
    # Xero Reference field has a hard 255-char limit
    final_reference = final_reference[:255]

    body = {
        "Type": "ACCPAY",  # AP / supplier bill
        # HARD LOCKED. NEVER change this here. Tony 2026-05-27 — drafts only.
        "Status": "DRAFT",
        "Date": bill_date or _dt.date.today().isoformat(),
        "Contact": contact_block,
        "Reference": final_reference,
        "LineItems": payload_lines,
        "LineAmountTypes": "Exclusive",
    }

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
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:1000]
        try:
            err_json = json.loads(err_body)
            msg = (
                err_json.get("Message")
                or err_json.get("Detail")
                or err_body
            )
        except json.JSONDecodeError:
            msg = err_body
        raise RuntimeError(f"Xero {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Xero unreachable: {e.reason}") from e

    invoice = data["Invoices"][0]
    inv_id = invoice["InvoiceID"]
    return {
        "ok": True,
        "entity": entity.upper(),
        "InvoiceID": inv_id,
        "InvoiceNumber": invoice.get("InvoiceNumber"),
        "Status": invoice.get("Status"),
        "Total": invoice.get("Total"),
        "SubTotal": invoice.get("SubTotal"),
        "TotalTax": invoice.get("TotalTax"),
        "Reference": invoice.get("Reference"),
        "Contact": (invoice.get("Contact") or {}).get("Name"),
        "xero_link": f"https://go.xero.com/AccountsPayable/Edit.aspx?InvoiceID={inv_id}",
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Create a DRAFT supplier bill in Xero.")
    p.add_argument("--entity", required=True, choices=["SC", "CQ", "sc", "cq"])
    p.add_argument(
        "--supplier",
        required=True,
        help="Supplier name (Xero will match-or-create) — or use --supplier-contact-id",
    )
    p.add_argument(
        "--supplier-contact-id",
        help="Xero ContactID (UUID). Takes precedence over --supplier name lookup.",
    )
    p.add_argument("--narration", help="Free text appended to the reference for context.")
    p.add_argument("--reference", help="Supplier's invoice/reference number.")
    p.add_argument("--date", help="ISO date (yyyy-mm-dd). Defaults to today (Brisbane).")
    p.add_argument(
        "--lines",
        required=True,
        help="JSON array — [{\"amount\": 500.0, \"account_code\": \"6010\", \"description\": \"…\", \"tax_type\": \"INPUT\"}]",
    )
    args = p.parse_args()

    try:
        lines = json.loads(args.lines)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"--lines must be JSON: {e}"}), flush=True)
        return 1

    try:
        result = create_draft_bill(
            entity=args.entity,
            supplier_name=args.supplier,
            supplier_contact_id=args.supplier_contact_id,
            lines=lines,
            narration=args.narration,
            bill_date=args.date,
            reference=args.reference,
        )
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
