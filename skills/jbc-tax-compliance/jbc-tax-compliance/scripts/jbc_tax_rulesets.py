"""Hardcoded ATO + QLD statutory constants for jbc-tax-compliance.

PHASE 2 DELIBERATE SIMPLIFICATION
=================================
The legacy `tax-compliance-agent` carried a versioned `TaxRuleSet` table
in Prisma so values could be loaded per effective date. For the Hermes
port we substitute hardcoded constants for the current statutory period.
When ATO/QLD rates change, bump this file and ship a new skill version.

Effective-from: 2025-07-01 (Australian FY 2025/26)

Sources:
  - GST rate: A New Tax System (Goods and Services Tax) Act 1999 — 10%.
  - SG rate: Superannuation Guarantee (Administration) Act 1992 —
    legislated to 12.0% from 1 July 2025 (final step in the schedule).
  - QLD payroll tax:
      Rate     — 4.75% (annual wages > $6.5m → 4.95%; mental health
                 levy of 0.25% above $10m / 0.5% above $100m).
      Threshold — $1,300,000 annual deduction, full up to $6.5m AGW,
                 then reduced $1 for every $4 above it (nil at $11.7m).
      JBC's grouped wages (~$13m) are past the phase-out, so there is no
      deduction at all. The detector's estimate uses the tiered rate and
      phase-out but NOT the levy, the regional discount or super-in-wages,
      so it is context only — see detectors/payroll_tax.py.
  - BAS quarterly due dates (no tax-agent extension):
      Q1 Jul–Sep → due 28 Oct
      Q2 Oct–Dec → due 28 Feb (ATO concession)
      Q3 Jan–Mar → due 28 Apr
      Q4 Apr–Jun → due 28 Jul
  - Super quarterly due: 28th of month after quarter end.
  - QLD payroll-tax monthly return: 7th of following month.

Audit caller: emit RULESET_VERSION + EFFECTIVE_FROM on every finding's
evidence so the persisted finding is replayable.
"""

from __future__ import annotations

RULESET_VERSION = "jbc-tax-2025-26-v2"
EFFECTIVE_FROM = "2025-07-01"

# ATO
ATO_GST_RATE: float = 0.10
ATO_SG_RATE: float = 0.12  # FY2025-26 onwards (capped)

# QLD payroll tax (headline scale)
QLD_PAYROLL_TAX_RATE: float = 0.0475
QLD_PAYROLL_TAX_HIGH_RATE: float = 0.0495
QLD_PAYROLL_TAX_HIGH_RATE_FROM_AUD: float = 6_500_000.0
QLD_PAYROLL_TAX_ANNUAL_THRESHOLD_AUD: float = 1_300_000.0
QLD_PAYROLL_TAX_PHASEOUT_FROM_AUD: float = 6_500_000.0

# Behaviour thresholds (overridable via env in the orchestrator).
DEFAULT_DUE_DATE_CRITICAL_DAYS = 7
DEFAULT_DUE_DATE_WARNING_DAYS = 30
DEFAULT_GST_CASH_WARNING_PCT = 0.80
DEFAULT_GST_CASH_CRITICAL_PCT = 0.50
DEFAULT_UNTAGGED_NET_THRESHOLD_AUD = 1_000.0
DEFAULT_LOOKBACK_DAYS = 120


def ruleset_meta() -> dict[str, str | float]:
    """Stamp this on every finding's evidence for auditability."""
    return {
        "rulesetVersion": RULESET_VERSION,
        "rulesetEffectiveFrom": EFFECTIVE_FROM,
        "gstRate": ATO_GST_RATE,
        "sgRate": ATO_SG_RATE,
        "qldPayrollTaxRate": QLD_PAYROLL_TAX_RATE,
        "qldPayrollTaxAnnualThreshold": QLD_PAYROLL_TAX_ANNUAL_THRESHOLD_AUD,
    }
