# Data contracts

For the proposed real AWS integration, see [AWS evidence platform](AWS_DATA_PLATFORM.md).
The fixture contracts below remain implemented synthetic contracts; they are not native AWS export schemas.

The cloud pilot's additional contracts are documented in [scoped Athena tools](SCOPED_ATHENA_AGENTCORE.md)
and the [common resource/opportunity model](COMMON_DATABASE.md). They remain synthetic pilot contracts.

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

## 6. Optimization snapshot (local source)

```
<root>/optimization/001-2026-11-02/
  manifest.json
  coh-recommendations.csv
  compute-optimizer-ec2.csv
```

**Synthetic. Column names are a subset of the real AWS exports, written from the documented schemas. They have not
been tested against real files.** A real adapter must be tested before we claim support.

### manifest.json (schemaVersion `opt-subset-v1`)

| Field | Type | Meaning |
|---|---|---|
| schemaVersion | string | must be `opt-subset-v1` |
| syntheticData | bool | true for generated fixtures (informational) |
| executionId | string | unique per snapshot; same id + same content = duplicate |
| exportTimestamp | `YYYY-MM-DDTHH:MM:SSZ` | newer timestamp wins |
| dataFiles | list of `{key, sha256, rowCount, table}` | `table` is `coh` or `compute_optimizer_ec2`; each at most once; at most 5,000 rows |

A snapshot is a point-in-time view: a newer one replaces the previous one completely. Outcomes match section 3
(`active`, `superseded`, `duplicate`, `stale`, `rejected_*`). One bad row rejects the whole snapshot and keeps the
previous one.

### Cost Optimization Hub columns (`table: coh`)

`recommendation_id, account_id, region, resource_arn, action_type, currency_code, current_resource_type,
current_resource_summary, recommended_resource_type, recommended_resource_summary, recommended_resource_details,
recommendation_source, estimated_monthly_cost_before_discount, estimated_monthly_cost_after_discount,
estimated_monthly_savings_before_discount, estimated_monthly_savings_after_discount, implementation_effort,
restart_needed, rollback_possible, recommendation_lookback_period_in_days, last_refresh_timestamp`

- `action_type`: Rightsize, Stop, Delete, Upgrade, MigrateToGraviton, ScaleIn, PurchaseSavingsPlans, PurchaseReservedInstances.
- `implementation_effort`: VeryLow, Low, Medium, High, VeryHigh.
- Money: decimal strings, USD only, never negative. Parsed once to integer nano-USD.
- `resource_arn` is empty for commitments. `recommended_resource_details` is a JSON object; for a commitment it holds
  `hourlyCommitment`, `term` and `paymentOption`.

### Compute Optimizer EC2 columns (`table: compute_optimizer_ec2`)

`accountId, instanceArn, finding, currentInstanceType, currentOnDemandPrice, utilizationMetrics_CPU_MAXIMUM,
utilizationMetrics_MEMORY_MAXIMUM, recommendationOptions_1_instanceType, recommendationOptions_1_performanceRisk,
recommendationOptions_1_estimatedMonthlySavings_value, recommendationOptions_1_estimatedMonthlySavings_currency,
recommendationOptions_1_migrationEffort, lookbackPeriodInDays, lastRefreshTimestamp`

- `finding`: OVER_PROVISIONED, UNDER_PROVISIONED, OPTIMIZED.
- `currentOnDemandPrice` is per hour. Savings are on demand, before any discount.
- An empty memory metric means memory was not measured. Performance risk runs from 0 to 4.

### Evidence rules

| Rule | Value |
|---|---|
| Evidence window | last 30 days of CUR |
| Monthly run-rate | amortized cost x 730 / (days with usage x 24) |
| Minimum recommendation lookback | 14 days, else needs evidence |
| Snapshot older than 14 days | confidence falls to low |
| Source savings above run-rate | needs evidence |
| Source cost differs from CUR by more than 10% | confidence falls to low; for a commitment baseline, needs evidence |
| Stop | needs peak CPU of 10% or less |
| Rightsize, Stop, ScaleIn, Graviton | need an OVER_PROVISIONED Compute Optimizer row |

## 7. Organization and policies (local source)

```
<root>/governance/
  organization.json
  policies.json
```

**Synthetic, and our own contract.** The organization file is not an AWS export. A real adapter would build it from
AWS Organizations, and that needs separate approval. A bad file is rejected whole and the previous data is kept.

### organization.json (schemaVersion `org-subset-v1`)

| Field | Type | Meaning |
|---|---|---|
| managementAccount | 12 digits | SCPs have no effect on it |
| organizationalUnits | list of `{id, name, accounts}` | `id` like `ou-sandbox`; an account appears once |
| serviceControlPolicies | list of `{id, name, targets}` | existing SCPs; a target is `root`, an OU id or an account |

### policies.json (schemaVersion `policy-subset-v1`)

Each policy is `{id, name, kind, params}`. Unknown fields are rejected. There is no free-form logic.

| kind | params | Violation | Draft |
|---|---|---|---|
| required_tag | `key` | Usage rows without the tag | Tag policy |
| allowed_regions | `regions` | Usage outside the list (`global` is ignored) | SCP |
| restricted_instance | `accounts`, `families` | `BoxUsage` of those families | SCP |
| budget_coverage | none | Account with no budget covering all its spend | None |

### Governance rules

| Rule | Value |
|---|---|
| Evidence window | last 30 days of CUR, amortized cost |
| Spend total | each resource once; budget coverage left out |
| SCP size limit | 5,120 bytes |
| Tag policy size limit | 2,500 bytes |
| SCPs per target | 5 |
| Exception | needs owner, reason, expiry within 365 days; active through its expiry date |

The AWS limits above were written from memory of the AWS documentation. Confirm them before any real use.

## 8. Savings verification

### Recorded change (`opt_changes`)

| Field | Meaning |
|---|---|
| finding_id, recommendation_id, title | which finding was acted on |
| account_id, resource_id, action, recommended | frozen when recorded, so a later snapshot cannot change them |
| estimate_nanos | the source estimate, frozen when recorded |
| implemented_on | `YYYY-MM-DD`, not in the future; one change per resource per day |
| note | optional, at most 500 characters |

Only Plan ready, Conditional and Alternative findings can be recorded. Realised numbers are never stored.

### Rules

| Rule | Value |
|---|---|
| Baseline | up to 14 days before the change, never before a previous change on the resource |
| Measured period | at least 7 days; ends at the next change on the resource; final billing used when it gives 7 days |
| Usage measured from | `line_item_usage_amount` on Usage and SavingsPlanCoveredUsage rows |
| Expected, no change | baseline cost x after days / baseline days |
| Expected, old rate | baseline cost x after usage / baseline usage |
| Usage effect | expected no change - expected old rate |
| Rate effect | expected old rate - actual |
| Credited | Stop, Delete: both effects. Others: rate effect only |
| Commitment effect | daily growth in unused Savings Plan commitment, for covered resources, deducted once by date |
| Credits, refunds | shown, not counted |
| Verified | at least 80% of the estimate on final billing, and the expected change shows in CUR |
| Partly realised | 5% to 80% of the estimate, or the expected change does not show in CUR |
| Not realised | below 5% of the estimate |
| Provisional | measured on billing that is not final |
| Waiting | fewer than 7 days after the change |
| Cannot verify | missing month, short baseline, charges that cannot be amortized, or no usage after a non-stop change |

### Savings scenario (`demo --scenario savings`)

A separate database. It records 9 changes on the normal fixtures, then loads `<root>/after-changes/`: November final and
December month-to-date, through 15 Dec 2026. Reference date 16 Dec 2026. Every outcome is planted.
## 9. Agent state

| Table | Holds | Scope | Kept |
|---|---|---|---|
| conversations, conversation_messages | Titles and plain-text turns, redacted | One user | 90 days after the last message |
| memories | Short notes a person added | One user | Until deleted; at most 50, 300 characters each |
| agent_runs | Status, tools, turns, tokens, time, question, answer, trace, feedback | One user; the summary is an operator view with no questions or answers | 90 days |
| reports | Saved answer, trace and evidence queries | One user | Until deleted; at most 200 |

- `COSTLENS_CHAT_RETENTION_DAYS` changes the 90 days. `COSTLENS_AGENT_MONTHLY_TOKENS` sets the model limit (default 20,000,000).
- At most the last 16 messages or 12,000 characters of a conversation reach the model.
- Memory refuses amounts and anything that looks like a secret.
- User identity is `local`, or `x-amzn-oidc-identity` from the load balancer. The token signature is not verified.
