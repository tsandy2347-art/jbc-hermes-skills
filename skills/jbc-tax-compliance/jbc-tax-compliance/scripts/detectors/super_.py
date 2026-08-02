"""Super-clearing position — Xero-only.

Like payg.py: the legacy agent compared calculated SG against payroll
batches. With no MYOB API we only report the Xero super-clearing balance.
"""

from __future__ import annotations

import os
from typing import Any

from scripts import xero_tax
from scripts.jbc_tax_rulesets import ATO_SG_RATE, ruleset_meta
from scripts.periods import today_bne


def run_super(entity: str) -> list[dict[str, Any]]:
    if not xero_tax.tenant_configured(entity):
        return []

    codes = xero_tax.split_codes(
        os.environ.get(f"XERO_{entity}_SUPER_ACCOUNT_CODES")
    )
    if not codes:
        return [{
            "detector": "super-clearing-position",
            "domain": "super",
            "severity": "warning",
            "entity_code": entity,
            "title": f"[{entity}] Super clearing account not mapped",
            "detail": (
                f"{entity} cannot report a super clearing balance — "
                f"XERO_{entity}_SUPER_ACCOUNT_CODES is not set. Late super is "
                f"non-deductible and triggers the SG charge — keep this mapped."
            ),
            "evidence": {
                "dedupKey": f"super-clearing-position:{entity}:unmapped",
                "entityCode": entity,
                "kind": "unmapped",
                "sgRate": ATO_SG_RATE,
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
        # None of the configured codes exist on this entity's chart of accounts
        return [{
            "detector": "super-clearing-position",
            "domain": "super",
            "severity": "warning",
            "entity_code": entity,
            "title": f"[{entity}] Super clearing balance unreadable",
            "detail": (
                f"{entity}: configured super account codes {codes} do not exist "
                f"on the chart of accounts. Confirm the codes are correct."
            ),
            "evidence": {
                "dedupKey": f"super-clearing-position:{entity}",
                "entityCode": entity,
                "codesTried": codes,
                **ruleset_meta(),
            },
        }]

    return [{
        "detector": "super-clearing-position",
        "domain": "super",
        "severity": "info",
        "entity_code": entity,
        "title": f"[{entity}] Super clearing balance: ${total:.2f}",
        "detail": (
            f"{entity} super clearing balance at {today.isoformat()} = "
            f"${total:.2f} across {found_in_coa} account(s)"
            + (" (account at zero — cleared)" if found_in_tb == 0 else "")
            + f". SG rate {ATO_SG_RATE * 100:.2f}%. "
            f"Compare to payroll-system super liability outside the skill."
        ),
        "amount": round(total * 100) / 100,
        "evidence": {
            "dedupKey": f"super-clearing-position:{entity}",
            "entityCode": entity,
            "asAt": today.isoformat(),
            "totalAbs": round(total * 100) / 100,
            "accounts": per_account,
            "foundInTrialBalance": found_in_tb,
            "zeroBecauseNotInTb": found_in_coa - found_in_tb,
            "sgRate": ATO_SG_RATE,
            **ruleset_meta(),
        },
    }]
