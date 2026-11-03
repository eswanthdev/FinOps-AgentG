# Data contracts

## 1. Delivery folder (local source)

```
<root>/
  SYNTHETIC_FIXTURES            marker file (only for generated fixtures)
  001-2026-08-final/
    manifest.json
    part-00001.csv
    part-00002.csv
  002-2026-09-initial/ ...
```

Each folder is one export execution, processed in sorted name order (simulated arrival order).

### manifest.json (schemaVersion `cur2-subset-v1`)

| Field | Type | Meaning |
|---|---|---|
| schemaVersion | string | must be `cur2-subset-v1` |
| syntheticData | bool | true for generated fixtures (informational) |
| exportName | string | export identity; `[A-Za-z0-9._:-]{1,128}` |
| executionId | string | unique per export run; same id + same content = duplicate |
| billingPeriod | `YYYY-MM` | the period this delivery fully restates |
| exportTimestamp | ISO-8601 UTC | newer timestamp wins for the same period |
| periodComplete | bool | true when AWS has finalised the period |
| dataFiles | list of `{key, sha256, rowCount}` | every file must exist, match checksum and row count |

**This is our contract, not the real AWS Data Exports manifest.** A future S3 adapter must map the real manifest
to these fields and must be tested against real exports before we claim support.

## 2. CUR row (CSV, CUR 2.0 column names, subset)

`identity_line_item_id, bill_billing_period_start_date, bill_payer_account_id, line_item_usage_account_id,
line_item_usage_account_name, line_item_line_item_type, line_item_usage_start_date, line_item_usage_end_date,
line_item_product_code, line_item_usage_type, line_item_operation, line_item_resource_id, line_item_usage_amount,
line_item_unblended_cost, pricing_public_on_demand_cost, product_region_code,
savings_plan_savings_plan_effective_cost, savings_plan_total_commitment_to_date, savings_plan_used_commitment,
resource_tags`

- `resource_tags` is a JSON object of string → string (a map column in real Parquet exports), e.g.
  `{"user:team":"checkout"}`.
- Timestamps are ISO-8601, UTC. A row's usage start must fall inside its billing period.
- Amounts are decimal strings in USD. Rows are rounded once to 9 decimal places (half-even) at ingestion.
- Account ids must be 12 digits. `identity_line_item_id` must be unique within a delivery.
- Granularity in the fixtures is daily. Hourly rows would work; queries bucket by usage start date.

### Amortization rules

| line_item_line_item_type | unblended | amortized |
|---|---|---|
| Usage, Tax, Fee, Credit, Refund | as billed | = unblended |
| SavingsPlanCoveredUsage | on-demand equivalent | savings_plan_savings_plan_effective_cost |
| SavingsPlanNegation | negative of covered | 0 |
| SavingsPlanRecurringFee | commitment | total_commitment_to_date − used_commitment (unused part) |
| anything else (RIFee, DiscountedUsage, …) | as billed | **NULL** → amortized queries return insufficient data |

Invariant tested: over full periods, amortized total = unblended total (the commitment is fully accounted for).

## 3. Delivery outcomes

| Outcome | Rule | Effect |
|---|---|---|
| active | valid, first or newer than current for the period | period rows replaced in one transaction |
| superseded | was active, replaced by a newer revision | its rows are gone |
| duplicate | same executionId, identical fingerprint | skipped |
| stale | older exportTimestamp than the active delivery | skipped |
| rejected_incomplete | missing file, checksum or row-count mismatch, bad gzip | nothing loaded; previous kept |
| rejected_invalid | bad manifest, invalid row, duplicate line id | nothing loaded |
| rejected_conflict | same executionId seen with different content | nothing loaded |
| rejected_access | permission denied, or a file key escaping the folder | nothing loaded |

Coverage per billing period: `final`, `in_progress` (not final, may be revised), `missing` (expected but no
delivery), `before_coverage`, `future`.

## 4. CostQuery (API and tool input)

```json
{
  "kind": "explore | variance",
  "start": "YYYY-MM-DD", "end": "YYYY-MM-DD (exclusive)",
  "metric": "unblended | amortized",
  "granularity": "daily | monthly | total",
  "group_by": "service | account | region | usage_type | line_item_type | resource | tag:<key> | null",
  "filters": { "<dimension>": ["value", "..."] },
  "top_n": 1-20,
  "chart": "auto | stacked | line | bar | hbar | table",
  "compare_start": "YYYY-MM-DD", "compare_end": "YYYY-MM-DD"
}
```

Limits: ranges ≤ 400 days, ≤ 8 filter dimensions, ≤ 50 values each, values ≤ 200 characters, unknown fields
rejected. Tag filter value `(untagged)` matches rows without that tag.

## 5. Synthetic fixture story

Four accounts under payer `110987654321`: prod-commerce, prod-data, staging, sandbox. Data Aug 1 – Nov 2 2026.
July 2026 is intentionally missing. Built-in stories: prod-data ETL fleet (8 × c6i.4xlarge) and NAT data
processing jump from 15 Oct; an untagged sandbox GPU instance since 20 Sep; a Compute Savings Plan in
prod-commerce; a September revision with a −$250 credit.
