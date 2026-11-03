import pytest

from costlens.ingest.cur2 import REQUIRED_COLUMNS, RowError, parse_row

BASE = {c: "" for c in REQUIRED_COLUMNS}
BASE.update({
    "identity_line_item_id": "li-1",
    "bill_billing_period_start_date": "2026-10-01T00:00:00Z",
    "bill_payer_account_id": "110987654321",
    "line_item_usage_account_id": "210987654321",
    "line_item_usage_account_name": "prod-commerce",
    "line_item_line_item_type": "Usage",
    "line_item_usage_start_date": "2026-10-05T00:00:00Z",
    "line_item_usage_end_date": "2026-10-06T00:00:00Z",
    "line_item_product_code": "AmazonEC2",
    "line_item_usage_type": "BoxUsage:m6i.xlarge",
    "line_item_unblended_cost": "4.608",
    "pricing_public_on_demand_cost": "4.608",
    "product_region_code": "us-east-1",
    "resource_tags": "{}",
})


def row(**kw):
    r = dict(BASE)
    r.update(kw)
    return r


def test_usage_amortized_equals_unblended():
    r = parse_row(row(), "2026-10")
    assert r.unblended_nanos == r.amortized_nanos == 4_608_000_000


def test_savings_plan_lines():
    covered = parse_row(row(line_item_line_item_type="SavingsPlanCoveredUsage",
                            savings_plan_savings_plan_effective_cost="3.2256"), "2026-10")
    assert covered.unblended_nanos == 4_608_000_000 and covered.amortized_nanos == 3_225_600_000
    neg = parse_row(row(line_item_line_item_type="SavingsPlanNegation", line_item_unblended_cost="-4.608"), "2026-10")
    assert neg.amortized_nanos == 0
    fee = parse_row(row(line_item_line_item_type="SavingsPlanRecurringFee", line_item_unblended_cost="14",
                        savings_plan_total_commitment_to_date="14", savings_plan_used_commitment="12.9024"), "2026-10")
    assert fee.amortized_nanos == 1_097_600_000


def test_used_commitment_above_total_is_rejected():
    with pytest.raises(RowError):
        parse_row(row(line_item_line_item_type="SavingsPlanRecurringFee", line_item_unblended_cost="14",
                      savings_plan_total_commitment_to_date="14", savings_plan_used_commitment="15"), "2026-10")


def test_unsupported_type_has_no_amortized_value():
    r = parse_row(row(line_item_line_item_type="RIFee"), "2026-10")
    assert r.amortized_nanos is None


@pytest.mark.parametrize("kw,msg", [
    ({"bill_billing_period_start_date": "2026-09-01T00:00:00Z"}, "does not match"),
    ({"line_item_usage_start_date": "2026-11-01T00:00:00Z", "line_item_usage_end_date": "2026-11-02T00:00:00Z"}, "outside"),
    ({"line_item_usage_account_id": "12345"}, "12-digit"),
    ({"resource_tags": "{not json"}, "JSON"),
    ({"resource_tags": '{"a": 1}'}, "string"),
    ({"line_item_unblended_cost": "abc"}, "decimal"),
    ({"line_item_usage_end_date": "2026-10-04T00:00:00Z"}, "before"),
    ({"identity_line_item_id": ""}, "empty"),
])
def test_invalid_rows(kw, msg):
    with pytest.raises(RowError, match=msg):
        parse_row(row(**kw), "2026-10")


def test_missing_column():
    r = row()
    del r["resource_tags"]
    with pytest.raises(RowError, match="missing columns"):
        parse_row(r, "2026-10")
