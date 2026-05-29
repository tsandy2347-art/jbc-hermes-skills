"""ATO ABN checksum validation.

Port of /Users/anthonysandy/Finance/payables-agent/lib/payables/abn.ts.

Algorithm:
    1. Subtract 1 from the first digit.
    2. Multiply each digit by [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19].
    3. Sum the products. Valid if sum mod 89 == 0.
"""

from __future__ import annotations

ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)


def normalise_abn(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) != 11:
        return None
    return digits


def is_valid_abn(raw: str | None) -> bool:
    digits = normalise_abn(raw)
    if not digits:
        return False
    d = [int(x) for x in digits]
    d[0] -= 1
    if d[0] < 0:
        return False
    s = sum(d[i] * ABN_WEIGHTS[i] for i in range(11))
    return s % 89 == 0
