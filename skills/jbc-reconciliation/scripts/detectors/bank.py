"""Domain A — bank reconciliation + cash detectors.

Emits:
  bank-overdraft           (critical)
  bank-low-cash            (warning, only if a threshold env var is set)
  balance-unavailable      (critical)

NOTE on stale-unreconciled (REMOVED 2026-06-07):
  Xero's BankTransactions API returns IsReconciled=false for transactions
  that the Xero UI shows as fully reconciled (e.g. anything matched via
  Cash Coding or Bank Statement Lines). Summing those produces nonsense
  totals — Westpac NDIS Acc 1432 was being reported as "$35M unreconciled"
  when the actual statement-vs-ledger drift is ~$52k.

  There is no clean Xero API endpoint that exposes the Statement Balance
  to compare against the GL balance, so we cannot calculate the real
  drift programmatically. Detector removed entirely rather than emit
  misleading numbers. Real bank-rec issues will surface via
  bank-overdraft / balance-unavailable / monthly trial balance review.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from ..xero_client import (
    bank_summary,
    list_bank_accounts,
    mask_account,
)


def _fmt_aud(n: float) -> str:
    return f"A${n:,.2f}"


def _business_days_between(a: _dt.datetime, b: _dt.datetime) -> int:
    """Mon–Fri days strictly between dates (inclusive of weekday count). Naive but fine for AEST."""
    if a > b:
        a, b = b, a
    days = (b.date() - a.date()).days
    if days <= 0:
        return 0
    full_weeks, rem = divmod(days, 7)
    count = full_weeks * 5
    start_dow = a.weekday()
    for i in range(rem):
        if (start_dow + i) % 7 < 5:
            count += 1
    return count


def _low_cash_threshold(entity: str) -> float | None:
    raw = os.environ.get(f"RECON_LOW_CASH_WARNING_{entity.upper()}_AUD")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# Which accounts constitute each entity's "cash position" — the Westpac
# operating accounts only, per Tony (2026-07-08): credit cards and
# participant/trust (Vasco) balances are excluded so the number is JBC's own
# operating cash. Matched by name substring (the embedded account number keeps
# it unambiguous). Overridable via RECON_CASH_ACCOUNTS_<ENTITY> (comma-sep).
_CASH_ACCOUNT_DEFAULTS = {
    "SC": ["Main Acc 1205", "NDIS Acc 1432", "HCP Acc 1440"],
    "CQ": ["Business One"],
}


def _cash_account_patterns(entity: str) -> list[str]:
    raw = os.environ.get(f"RECON_CASH_ACCOUNTS_{entity.upper()}")
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return _CASH_ACCOUNT_DEFAULTS.get(entity.upper(), [])


def _is_cash_account(entity: str, name: str) -> bool:
    n = _norm(name)
    return any(_norm(p) in n for p in _cash_account_patterns(entity))


def _norm(s: Any) -> str:
    """Collapse whitespace + lowercase for tolerant name comparison."""
    return " ".join(str(s or "").split()).strip().lower()


def _name_matches(report_name: str, account_name: str) -> bool:
    """Match a BankSummary row name to an Accounts-endpoint account name.

    Xero's BankSummary report TRUNCATES account names to ~30 chars and carries
    NO account-id attribute, so the old exact-name / by-id match dropped ~40%
    of accounts (every long-named account → false "balance unavailable"). The
    report name is a leading prefix of the full account name, so we match on:
      - exact (after whitespace/case normalisation), or
      - the full account name STARTS WITH the (truncated) report name.
    A minimum length guards against a short generic name ("Credit Card")
    prefix-matching the wrong row.
    """
    rn, an = _norm(report_name), _norm(account_name)
    if not rn or not an:
        return False
    if rn == an:
        return True
    return len(rn) >= 8 and an.startswith(rn)


def _find_balance(rows: list[dict[str, Any]], account_id: str, account_name: str) -> float | None:
    for row in rows or []:
        if row.get("Rows"):
            inner = _find_balance(row["Rows"], account_id, account_name)
            if inner is not None:
                return inner
        if row.get("RowType") == "Row":
            cells = row.get("Cells") or []
            if not cells:
                continue
            first = cells[0]
            attrs = first.get("Attributes") or []
            # by_id kept for forward-compat, but BankSummary currently emits no
            # account-id attribute, so the name match below is what actually works.
            by_id = bool(account_id) and any(
                a.get("Id") == "account" and a.get("Value") == account_id for a in attrs
            )
            if by_id or _name_matches(first.get("Value", ""), account_name):
                # Closing balance is the last numeric cell.
                for i in range(len(cells) - 1, -1, -1):
                    v = cells[i].get("Value", "")
                    try:
                        return float(str(v).replace(",", "").replace(" ", ""))
                    except (ValueError, TypeError):
                        continue
    return None


def _extract_balance(bs_report: dict[str, Any] | None, account: dict[str, Any]) -> float | None:
    if not bs_report:
        return None
    reports = bs_report.get("Reports") or []
    if not reports:
        return None
    rows = reports[0].get("Rows") or []
    return _find_balance(rows, account.get("AccountID", ""), account.get("Name", ""))


def run_bank(entity: str, *, lookback_days: int = 0, unmatched_days: int = 0) -> list[dict[str, Any]]:  # noqa: ARG001
    """Returns a list of finding-dicts ready for the orchestrator to persist.

    lookback_days and unmatched_days are kept in the signature for backward
    compatibility with the orchestrator but are no longer consumed since the
    stale-unreconciled detector was removed.
    """
    findings: list[dict[str, Any]] = []
    today = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=10))  # AEST
    )
    today_iso = today.date().isoformat()
    week_ago_iso = (today - _dt.timedelta(days=7)).date().isoformat()

    try:
        accounts = list_bank_accounts(entity)
    except Exception as exc:  # noqa: BLE001
        findings.append({
            "detector": "balance-unavailable",
            "domain": "bank",
            "severity": "critical",
            "entity_code": entity,
            "title": f"{entity}: bank accounts could not be listed",
            "detail": f"Xero Accounts endpoint failed: {exc}. Treat as eyes-on until resolved.",
            "amount": None,
            "evidence": {"dedupKey": f"bank-list-failed:{entity}",
                         "kind": "balance-unavailable", "error": str(exc)},
        })
        return findings

    try:
        bs = bank_summary(entity, week_ago_iso, today_iso)
    except Exception:
        bs = None

    low_cash = _low_cash_threshold(entity)
    cash_parts: list[tuple[str, float]] = []  # (name, balance) for cash-position

    for acc in accounts:
        # Archived accounts appear in the CoA but not in BankSummary — matching
        # them always fails and yields a false "balance unavailable". Skip them;
        # the auto-resolve sweep clears any lingering finding for the account.
        if str(acc.get("Status", "")).upper() == "ARCHIVED":
            continue

        acc_id = acc.get("AccountID", "")
        name = acc.get("Name", "(unnamed)")
        masked = mask_account(acc.get("BankAccountNumber"))
        is_cc = str(acc.get("BankAccountType", "")).upper() == "CREDITCARD"

        balance = _extract_balance(bs, acc)
        if balance is None:
            # The account has no row in BankSummary — Xero only lists accounts
            # with activity in the window, so this is almost always a dormant /
            # zero-activity account, NOT a monitoring failure. Emit a low-severity
            # note (never a critical "eyes-on") so it can't masquerade as a cash
            # emergency; a genuinely live account showing up here is worth a
            # manual glance but is not today's headline.
            findings.append({
                "detector": "balance-unavailable",
                "domain": "bank",
                "severity": "info",
                "entity_code": entity,
                "title": f"{name} {masked} — no recent activity",
                "detail": "No entry in Xero BankSummary for the reporting window — "
                          "likely a dormant or zero-activity account. Confirm manually "
                          "only if this account should be transacting.",
                "amount": None,
                "evidence": {
                    "dedupKey": f"balance-unavailable:{entity}:{acc_id}",
                    "kind": "balance-unavailable",
                    "xeroAccountId": acc_id, "accountName": name,
                },
            })
        else:
            if _is_cash_account(entity, name):
                cash_parts.append((name, balance))
            overdrawn = balance < 0
            if overdrawn and not is_cc:
                findings.append({
                    "detector": "bank-overdraft",
                    "domain": "bank",
                    "severity": "critical",
                    "entity_code": entity,
                    "title": f"{name} {masked} is overdrawn",
                    "detail": f"Current balance {_fmt_aud(balance)}. This must be addressed today.",
                    "amount": balance,
                    "evidence": {
                        "dedupKey": f"bank-overdraft:{entity}:{acc_id}",
                        "kind": "bank-overdraft",
                        "xeroAccountId": acc_id, "accountName": name,
                        "balance": balance,
                    },
                })
            elif not overdrawn and low_cash is not None and balance < low_cash:
                findings.append({
                    "detector": "bank-low-cash",
                    "domain": "bank",
                    "severity": "warning",
                    "entity_code": entity,
                    "title": f"{name} {masked} below low-cash threshold",
                    "detail": f"Balance {_fmt_aud(balance)} is below the {_fmt_aud(low_cash)} warning floor.",
                    "amount": balance,
                    "evidence": {
                        "dedupKey": f"bank-low-cash:{entity}:{acc_id}",
                        "kind": "bank-low-cash",
                        "xeroAccountId": acc_id, "accountName": name,
                        "balance": balance, "threshold": low_cash,
                    },
                })

        # stale-unreconciled removed — see module docstring for why.

    # ── Cash position: the entity's Westpac operating accounts, summed. ──
    # Info-severity data finding consumed by Mark's brief "CASH POSITION"
    # panel (recentCashByEntity). NOT an exception — brief.ts excludes the
    # cash-position detector from the action pipeline so it never headlines.
    if cash_parts:
        total = sum(b for _, b in cash_parts)
        breakdown = "; ".join(f"{nm}: {_fmt_aud(bal)}" for nm, bal in cash_parts)
        findings.append({
            "detector": "cash-position",
            "domain": "bank",
            "severity": "info",
            "entity_code": entity,
            "title": f"{entity} cash position: {_fmt_aud(total)}",
            "detail": f"Sum of Westpac operating accounts ({len(cash_parts)}): {breakdown}.",
            "amount": total,
            "evidence": {
                "dedupKey": f"cash-position:{entity}",
                "kind": "cash-position",
                "accounts": [{"name": nm, "balance": bal} for nm, bal in cash_parts],
            },
        })

    return findings
