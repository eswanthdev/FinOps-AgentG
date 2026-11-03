"""Deterministic money handling.

All stored amounts are integers in nano-USD (1 USD = 1_000_000_000 nanos).
CUR line items carry up to ~10 decimal places; we round each input value to
9 decimal places with ROUND_HALF_EVEN once, at ingestion. Sums are then exact
integer arithmetic (SQLite INTEGER SUM is exact). Rounding to cents happens
only for display, never before aggregation.

Floats are never used for money.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, InvalidOperation

NANOS_PER_USD = 1_000_000_000
_NANO = Decimal("0.000000001")
_CENT = Decimal("0.01")

# Guard rail: a single CUR line above 10 billion USD is treated as corrupt input.
MAX_ABS_LINE_USD = Decimal("10000000000")


class MoneyError(ValueError):
    pass


def parse_usd_to_nanos(value: str | None, *, allow_empty: bool = False) -> int:
    """Parse a CUR decimal string into integer nano-USD."""
    if value is None or str(value).strip() == "":
        if allow_empty:
            return 0
        raise MoneyError("empty monetary value")
    text = str(value).strip()
    try:
        d = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyError(f"not a decimal number: {text!r}") from exc
    if not d.is_finite():
        raise MoneyError(f"non-finite monetary value: {text!r}")
    if abs(d) > MAX_ABS_LINE_USD:
        raise MoneyError(f"monetary value out of range: {text!r}")
    return int((d.quantize(_NANO, rounding=ROUND_HALF_EVEN) * NANOS_PER_USD).to_integral_value())


def nanos_to_decimal(nanos: int) -> Decimal:
    return Decimal(int(nanos)) / Decimal(NANOS_PER_USD)


def nanos_to_cents_str(nanos: int) -> str:
    """Exact display value rounded half-even to cents, e.g. '1234.57'."""
    return str(nanos_to_decimal(nanos).quantize(_CENT, rounding=ROUND_HALF_EVEN))


def pct_change(current_nanos: int, base_nanos: int) -> str | None:
    """Percentage change as a string with 1 decimal place, or None when base is 0."""
    if base_nanos == 0:
        return None
    pct = (Decimal(current_nanos - base_nanos) / Decimal(abs(base_nanos))) * 100
    return str(pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN))


def share_pct(part_nanos: int, total_nanos: int) -> str | None:
    if total_nanos == 0:
        return None
    pct = (Decimal(part_nanos) / Decimal(total_nanos)) * 100
    return str(pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN))
