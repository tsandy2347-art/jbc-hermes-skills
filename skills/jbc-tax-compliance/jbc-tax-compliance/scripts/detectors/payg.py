"""PAYG clearing position — Xero-only.

The legacy agent reconciled MYOB payroll W2 against Xero's PAYG-withholding
GL clearing balance. MYOB has no API, so we drop the variance check and
emit a Xero-only position: "what's sitting in the PAYG-withholding clearing
account right now?" Humans use this against their payroll system.
"""

from __future__ import annotations

import os
from typing import Any

from scripts import xero_tax
from scripts.jbc_tax_rulesets import ruleset_meta
from scripts.periods import today_bne


def run_payg(entity: str) -> list[dict[str, Any]]:
    if not xero_tax.tenant_configured(entity):
        return []

    codes = xero_tax.split_codes(
        os.environ.get(f"XERO_{entity}_PAYG_ACCOUNT_CODES")
    )
    if not codes:
        return [{
            "detector": "payg-clearing-position",
            "domain": "payg",
            "severity": "warning",
            "entity_code": entity,
            "title": f"[{entity}] PAYG-withholding clearing account not mapped",
            "detail": (
                f"{entity} cannot report a PAYG-withholding clearing balance — "
                f"XERO_{entity}_PAYG_ACCOUNT_CODES is not set. Set this to the "
                f"comma-separated Xero account codes used to hold withheld PAYG."
            ),
            "evidence": {
                "dedupKey": f"payg-clearing-position:{entity}:unmapped",
                "entityCode": entity,
                "kind": "unmapped",
                **ruleset_meta(),
            },
        }]

    today = today_bne()
    tb = xero_tax.trial_balance(entity, today.isoformat())
    accounts = xero_tax.list_accounts(entity)

    total = 0.0
    found_in_coa = 0
    found_in_tb = 0
    per_account: list[dict[str, Any]] = []

    for c in codes:
        acc = next((a for a in accounts if a.get("Code") == c), None)
        if acc is None:
            # Account code doesn't exist on this entity's CoA at all — skip
            continue
        found_in_coa += 1
        name = acc.get("Name")
        bal = xero_tax.find_account_balance(tb, c, name)
        if bal is None:
            # Account exists in CoA but not in trial balance — it's zero (cleared).
            # Xero omits zero-balance accounts from the TrialBalance report.
            bal = 0.0
        else:
            found_in_tb += 1
        total += abs(bal)
        per_account.append({"code": c, "name": name, "balance": bal})

    if found_in_coa == 0:
        return [{
            "detector": "payg-clearing-position",
            "domain": "payg",
            "severity": "warning",
            "entity_code": entity,
            "title": f"[{entity}] PAYG clearing balance unreadable",
            "detail": (
                f"{entity}: configured PAYG account codes {codes} do not exist "
                f"on the chart of accounts. Confirm the codes are correct."
            ),
            "evidence": {
                "dedupKey": f"payg-clearing-position:{entity}:{today.isoformat()}",
                "entityCode": entity,
                "codesTried": codes,
                **ruleset_meta(),
            },
        }]

    return [{
        "detector": "payg-clearing-position",
        "domain": "payg",
        "severity": "info",
        "entity_code": entity,
        "title": f"[{entity}] PAYG-withholding clearing balance: ${total:.2f}",
        "detail": (
            f"{entity} PAYG-withholding clearing balance at {today.isoformat()} = "
            f"${total:.2f} across {found_in_coa} account(s)"
            + (" (account at zero — cleared)" if found_in_tb == 0 else "")
            + ". Compare to the latest payroll "
            f"W2 figure outside the skill — MYOB is not integrated."
        ),
        "amount": round(total * 100) / 100,
        "evidence": {
            "dedupKey": f"payg-clearing-position:{entity}:{today.isoformat()}",
            "entityCode": entity,
            "asAt": today.isoformat(),
            "totalAbs": round(total * 100) / 100,
            "accounts": per_account,
            "foundInTrialBalance": found_in_tb,
            "zeroBecauseNotInTb": found_in_coa - found_in_tb,
            **ruleset_meta(),
        },
    }]
