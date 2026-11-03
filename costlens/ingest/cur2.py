"""CUR 2.0 (AWS Data Exports) row contract used by this slice.

This is a documented SUBSET of the CUR 2.0 standard export columns, flattened
to CSV. Column names follow CUR 2.0 naming. `resource_tags` is a JSON object
(in real Parquet exports it is a map column). See docs/DATA_CONTRACTS.md.

Amortization rules (amortized cost = cost spread over the period it covers):
  Usage, Tax, Fee, Credit, Refund  -> amortized = unblended
  SavingsPlanCoveredUsage          -> amortized = savings_plan_savings_plan_effective_cost
  SavingsPlanNegation              -> amortized = 0 (it only cancels covered usage)
  SavingsPlanRecurringFee          -> amortized = total_commitment_to_date - used_commitment
                                       (the unused part of the commitment)
Any other line item type (RIFee, DiscountedUsage, SavingsPlanUpfrontFee, ...)
is loaded with amortized cost = NULL. Queries that ask for amortized cost over
such rows return "insufficient evidence" instead of guessing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..money import MoneyError, parse_usd_to_nanos

REQUIRED_COLUMNS = (
    "identity_line_item_id",
    "bill_billing_period_start_date",
    "bill_payer_account_id",
    "line_item_usage_account_id",
    "line_item_usage_account_name",
    "line_item_line_item_type",
    "line_item_usage_start_date",
    "line_item_usage_end_date",
    "line_item_product_code",
    "line_item_usage_type",
    "line_item_operation",
    "line_item_resource_id",
    "line_item_usage_amount",
    "line_item_unblended_cost",
    "pricing_public_on_demand_cost",
    "product_region_code",
    "savings_plan_savings_plan_effective_cost",
    "savings_plan_total_commitment_to_date",
    "savings_plan_used_commitment",
    "resource_tags",
)

SIMPLE_TYPES = {"Usage", "Tax", "Fee", "Credit", "Refund"}
SUPPORTED_TYPES = SIMPLE_TYPES | {
    "SavingsPlanCoveredUsage",
    "SavingsPlanNegation",
    "SavingsPlanRecurringFee",
}


class RowError(ValueError):
    pass


@dataclass(frozen=True)
class CurRow:
    line_item_id: str
    billing_period: str  # YYYY-MM
    payer_account_id: str
    account_id: str
    account_name: str
    line_item_type: str
    usage_date: str  # YYYY-MM-DD (start date of the line, UTC)
    service: str
    usage_type: str
    operation: str
    resource_id: str
    region: str
    usage_amount: str
    unblended_nanos: int
    on_demand_nanos: int
    amortized_nanos: int | None
    tags_json: str


def _parse_ts(value: str, field: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise RowError(f"{field} is empty")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RowError(f"{field} is not an ISO timestamp: {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _account_id(value: str, field: str) -> str:
    text = (value or "").strip()
    if len(text) != 12 or not text.isdigit():
        raise RowError(f"{field} must be a 12-digit AWS account id")
    return text


def _amortized(line_type: str, raw: dict[str, str], unblended: int) -> int | None:
    if line_type in SIMPLE_TYPES:
        return unblended
    if line_type == "SavingsPlanCoveredUsage":
        return parse_usd_to_nanos(raw["savings_plan_savings_plan_effective_cost"])
    if line_type == "SavingsPlanNegation":
        return 0
    if line_type == "SavingsPlanRecurringFee":
        total = parse_usd_to_nanos(raw["savings_plan_total_commitment_to_date"])
        used = parse_usd_to_nanos(raw["savings_plan_used_commitment"])
        if used > total:
            raise RowError("savings plan used commitment exceeds total commitment")
        return total - used
    return None


def parse_row(raw: dict[str, str], expected_period: str) -> CurRow:
    missing = [c for c in REQUIRED_COLUMNS if c not in raw]
    if missing:
        raise RowError(f"missing columns: {', '.join(missing)}")

    period_start = _parse_ts(raw["bill_billing_period_start_date"], "bill_billing_period_start_date")
    period = f"{period_start.year:04d}-{period_start.month:02d}"
    if period != expected_period:
        raise RowError(f"row billing period {period} does not match delivery period {expected_period}")

    start = _parse_ts(raw["line_item_usage_start_date"], "line_item_usage_start_date")
    end = _parse_ts(raw["line_item_usage_end_date"], "line_item_usage_end_date")
    if end < start:
        raise RowError("usage end date is before start date")
    if (start.year, start.month) != (period_start.year, period_start.month):
        raise RowError("usage start date falls outside the billing period")

    line_type = (raw["line_item_line_item_type"] or "").strip()
    if not line_type:
        raise RowError("line_item_line_item_type is empty")

    try:
        unblended = parse_usd_to_nanos(raw["line_item_unblended_cost"])
        on_demand = parse_usd_to_nanos(raw["pricing_public_on_demand_cost"], allow_empty=True)
        amortized = _amortized(line_type, raw, unblended)
    except MoneyError as exc:
        raise RowError(str(exc)) from exc

    tags_text = (raw["resource_tags"] or "").strip() or "{}"
    try:
        tags = json.loads(tags_text)
    except json.JSONDecodeError as exc:
        raise RowError("resource_tags is not valid JSON") from exc
    if not isinstance(tags, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items()):
        raise RowError("resource_tags must be a JSON object of string to string")

    line_id = (raw["identity_line_item_id"] or "").strip()
    if not line_id:
        raise RowError("identity_line_item_id is empty")

    return CurRow(
        line_item_id=line_id,
        billing_period=period,
        payer_account_id=_account_id(raw["bill_payer_account_id"], "bill_payer_account_id"),
        account_id=_account_id(raw["line_item_usage_account_id"], "line_item_usage_account_id"),
        account_name=(raw["line_item_usage_account_name"] or "").strip(),
        line_item_type=line_type,
        usage_date=start.date().isoformat(),
        service=(raw["line_item_product_code"] or "").strip() or "(unknown)",
        usage_type=(raw["line_item_usage_type"] or "").strip(),
        operation=(raw["line_item_operation"] or "").strip(),
        resource_id=(raw["line_item_resource_id"] or "").strip(),
        region=(raw["product_region_code"] or "").strip() or "global",
        usage_amount=(raw["line_item_usage_amount"] or "0").strip(),
        unblended_nanos=unblended,
        on_demand_nanos=on_demand,
        amortized_nanos=amortized,
        tags_json=json.dumps(tags, sort_keys=True, separators=(",", ":")),
    )


def period_bounds(period: str) -> tuple[date, date]:
    """Return [first day, first day of next month) for 'YYYY-MM'."""
    y, m = (int(x) for x in period.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return start, end
