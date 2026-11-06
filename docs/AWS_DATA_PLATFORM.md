# AWS evidence platform: contracts and implementation plan

Status: proposed design, 5 November 2026. This document does not describe deployed functionality.
Base branch: `feature/investigator-agent`. First milestone: a read-only, single-customer AWS pilot.

Pilot update: [synthetic Parquet in S3](SYNTHETIC_S3_PILOT.md). Five export buckets are used;
Storage Lens follows MCP instead of S3 by user preference. Production source buckets will be supplied by
the customer with read-only access and scoped Athena access; source creation is a test-environment activity only.

Implementation update: [local CUR CSV adapter](LOCAL_AWS_CUR.md) now handles explicitly listed local CSV/gzip
files with supplied metadata. This is part of step 2 only; native manifests, captured export validation,
COH mapping and S3 are still pending.

## 1. Architecture and boundaries

```text
Native exports ----> regional S3 landing locations ----+
Scheduled API collectors --> S3 raw snapshots --------+--> validate --> normalize --> publish
                                                                              |
                                                        curated Parquet / Glue / Athena
                                                                              |
CostLens UI --> authenticated application --> investigator on AgentCore Runtime |
                                              |                               |
                                              +--> typed FinOps query tools ---+
                                              +--> AgentCore Gateway / MCP --> selected read-only APIs
                                              |                                  |
                                              +--> Bedrock model                 +--> evidence snapshots
```

S3 holds evidence; Bedrock supplies the model; AgentCore hosts the agent and exposes governed tools.
Our code owns calculations, reconciliation, deduplication, validation and savings attribution.
The model receives bounded tool results, never complete exports or arbitrary SQL access.

Keep the current local SQLite demo working. Add production adapters behind explicit interfaces.
AgentCore is not a replacement for the billing engine or persistent application state.
Do not use SQLite on a transient agent runtime as the durable source of conversations or findings.
Select a managed state store before production hosting; preserve user/workspace scoping in its interface.

## 2. Source registry

Configuration is maintained by operators, not supplied by the model. Every enabled source has:

| Field | Contract |
|---|---|
| workspace_id | Trusted customer/workspace identity; bound to authenticated access |
| source_id, source_type | Stable registration and one of cur2, coh, compute_optimizer, storage_lens, trusted_advisor, api_snapshot |
| account_scope | Payer/member accounts explicitly included; never inferred as organization-wide |
| region_scope | Explicit regions, or global with the source's API endpoint region recorded separately |
| locations | Allow-listed bucket, prefix, bucket region and optional KMS key references |
| access_role | Role reference and external-id configuration reference where required; no credentials |
| export_identity | Native export/dashboard/check configuration identifier |
| schedule, freshness_target | Proposed collection cadence and maximum acceptable evidence age |
| required, enabled | Whether its absence blocks a particular capability |
| schema_adapter_version | Version of the source mapping |

Use separate regional landing buckets where a service requires them. A central logical catalog can query
these or publish normalized copies; one physical bucket is not a prerequisite.
Do not mix customer partitions under an unscoped query role.

## 3. S3 layout and delivery contract

Native exports retain their AWS-generated layout. Never rename their manifest fields to match our fixtures.
For copies, collectors and normalized outputs, use this application-owned convention:

```text
raw/workspace=<id>/source=<type>/account=<id>/region=<region>/run=<id>/...
curated/workspace=<id>/dataset=<name>/schema=<version>/period=<period>/generation=<id>/...
evidence/workspace=<id>/run=<id>/call=<id>/response.json
```

These prefixes are CostLens conventions, not native AWS paths. Readers only see published generations.
Preserve raw evidence immutably for a configured retention period; lifecycle and encryption are deployment decisions.
Where native objects are overwritten, preserve version IDs or copy a consistent generation before activation.
ETag is an object identifier, not a universally valid SHA-256 checksum.

Every adapter produces a normalized delivery envelope:

| Field | Meaning |
|---|---|
| delivery_id, workspace_id, source_id | Stable identity and ownership |
| native_execution_id | Native run identity if supplied; otherwise a documented deterministic identity |
| source_generated_at, collected_at, ingested_at | Separate UTC timestamps; unknown source time stays null |
| observed_start, observed_end | Evidence window, end exclusive; null if not provided |
| scope | Actual accounts, regions and resource types represented |
| objects | Bucket, key, version ID if available, size, computed checksum and native manifest reference |
| schema_version, adapter_version | Normalized schema and mapping version |
| completeness | complete, partial or unknown, with reasons |
| finality | final, provisional or unknown; generation time alone never proves final billing |
| status | discovered, validating, active, superseded, duplicate, stale or rejected |
| rejection_reason | incomplete, invalid, conflict, access or unsupported_schema |

Validate all expected files and API pages before publication. Partial snapshots must not replace complete ones.
Publish data and its active-generation pointer atomically at the catalog/application level.
Retries are idempotent. Conflicting contents under one delivery identity are rejected.
Use a work queue and bounded retries; events are hints and a periodic reconciliation scan finds missed deliveries.
Keep the previous active delivery on failure and show both its age and the rejected newer attempt.

## 4. Source-specific mappings

| Source | Collection | Normalized content | Replacement boundary |
|---|---|---|---|
| CUR 2.0 | Native Data Exports manifest and listed CSV/gzip or Parquet objects | Billing rows, accounts, charges, costs, usage, tags, resource IDs and commitment fields | Export identity + payer + billing period |
| Cost Optimization Hub | Native Data Exports | Recommendation identity, resource, action, current/recommended configuration, savings basis, currency, effort, refresh time and lookback when supplied | Export identity + declared complete scope |
| Compute Optimizer | Export job with CSV and metadata | Recommendations/options, resource identity, utilisation metrics with statistic and unit, performance risk and available lookback | Account scope + region + resource type + export configuration |
| Storage Lens | Daily CSV or Parquet metrics export | Dashboard/configuration ID, observation date, metric, value, unit, aggregation level and dimensions | Configuration + observation date + complete exported scope |
| Trusted Advisor | Scheduled, paginated API collector | Recommendation/check identity, category, status, affected resource and available source timestamps | Account/organization scope + API/check family |
| Savings Plans and other context | Typed API collectors and on-demand MCP wrappers | Request-specific result, native response metadata and timestamps | API + canonical request + access scope |

Required adapter rules:

- CUR: support real column mappings separately from `cur2-subset-v1`; unsupported charge/currency semantics
  produce an explicit unsupported/insufficient result. Preserve negative credits and refunds. Resource IDs can be absent.
  Never mark a real period final using only our synthetic `periodComplete` convention.
- COH: keep source savings, discount assumptions and commitment assumptions. Do not treat them as independently additive.
- Compute Optimizer: do not assume memory metrics exist or all resource types share the EC2 schema.
  A missing resource in a partial export does not establish deletion.
- Storage Lens: bucket/account/organization/prefix rows overlap. Select an aggregation level before summing.
  Store counters, rates and bytes with units; Storage Lens is not itself a USD savings estimate.
- Trusted Advisor: validate support-plan eligibility and organization access. Disabled/unavailable checks are not passing checks.
  Preserve source freshness rather than treating collector time as check refresh time.
- Commitment APIs: record lookback, term, payment option, account scope and generation time. Keep purchase proposals
  separate from savings counted after rightsizing. Unsupported scopes/filters must be rejected, not silently widened.

## 5. Common curated records

Create versioned schemas for `billing_items`, `recommendations`, `resource_metrics`, `resource_inventory`,
`commitment_snapshots`, `source_deliveries` and `evidence_objects`.

All records carry workspace/source/delivery identity. Correlate resources using partition, account, region,
service and native resource ID/ARN; unresolved links remain unresolved. Keep raw identifiers for traceability.
Recommendations retain their provider IDs and source evidence even when grouped into one opportunity.
An opportunity uses a stable resource + action-family identity and preserves decisions across refreshes.

Amounts are decimal strings on external boundaries and exact fixed-point values internally. Preserve existing
nano-USD semantics for supported USD data, with half-even display rounding. No float arithmetic for money.
Validate range/overflow and rounding parity when moving calculations to Athena; do not assume BIGINT aggregates
can represent arbitrary customer scale. Non-USD data must not be labelled USD or combined without an explicit FX contract.

Keep freshness per source and evidence window. Cost, metrics and recommendations can refer to different dates;
show those differences in confidence and blockers. Never invent daily utilisation from a summary metric.

## 6. Query and MCP tool contract

First preserve `CostQuery` and introduce a backend interface for run, coverage, dimensions, account names and
reference date. Existing workers also use direct SQLite queries: inventory and refactor these dependencies before
claiming Athena support across all modules. The synthetic demo retains its current reference-date behaviour;
real data uses an explicit as-of time and data-through watermark.

Initial live wrappers: `get_savings_plan_recommendations`, `get_commitment_coverage`,
`get_commitment_utilization`, `get_resource_configuration`, `get_resource_metrics` and `get_resource_tags`.
Exact AWS actions and supported resource types are selected and tested per wrapper before enablement.
No generic AWS-command, arbitrary SQL, or role-selection tool.

Each tool has a strict input schema, fixed IAM capability, account/region allow-list, maximum range,
pagination bound, output bound, timeout and collection budget. Workspace identity comes from verified application
context, never a model-controlled argument. Authorize every call, including cached reads.

Return this application-owned envelope:

```json
{
  "status": "ok",
  "evidence_id": "opaque-id",
  "source_id": "registered-source",
  "as_of": "2026-11-05T00:00:00Z",
  "observed_start": null,
  "observed_end": null,
  "scope": {"accounts": [], "regions": []},
  "freshness": "fresh",
  "completeness": "complete",
  "truncated": false,
  "warnings": [],
  "result": {}
}
```

Allowed result states: ok, insufficient_data, unsupported, unavailable and rejected.
`freshness` is fresh, stale or unknown, measured against source configuration, not a universal constant.
Never return status ok with a silently truncated aggregate. Provide bounded pagination or ask for narrower scope.
Snapshot successful live responses before treating them as durable evidence. Redact secrets and restrict evidence access.

Extend answer validation to bind amounts to evidence ID, resource/account scope, metric, sign, currency and period.
The current number-presence check is retained but is not proof that a claim is correct. History and memory are not evidence.

## 7. AgentCore integration and permissions

Wrap the existing investigator behind an authenticated Runtime entry point; preserve fake-model tests and local mode.
Expose approved FinOps tools through Gateway/MCP and keep deterministic computation in tool services.
Propagate verified user/workspace identity; trace application run, Gateway call, source delivery and query execution IDs.
Keep conversation/report state outside the runtime process. AgentCore Memory is optional and must preserve the existing
rule that only users write durable preferences; do not automatically migrate financial claims into memory.

Separate roles for source collection, normalization/publication, queries and agent invocation. The investigator has
no resource mutation or commitment purchase permissions. Ingestion may write only its evidence/curated destinations.
An Athena query role requires scoped workgroup/catalog/source access and a query-results location; it is not literally
S3-read-only. Configure query limits, result encryption, output retention and access boundaries before deployment.
Cross-account onboarding needs explicit customer grants and tenant isolation tests.

Later execution is a separate service with reviewed plans, fresh prechecks, attributable approval, scoped permissions,
idempotency, rollback and post-change measurement. No execution tool is included in this milestone.

## 8. Ordered implementation and acceptance gates

| Step | Deliverable | Acceptance gate |
|---|---|---|
| 1 | This design and source contracts | Mapping, boundaries and work order documented; no runtime capability claimed |
| 2 | Real CUR + COH adapters, initially using downloaded files | Sanitized native samples; schema/manifest validation; repeat ingestion and revision tests; incomplete delivery preserves active data |
| 3 | S3 reader and ingestion jobs | Injected-client tests for permissions, version changes, retries, pagination, duplicate events and recovery; real pilot read separately authorized |
| 4 | Compute Optimizer, Storage Lens, TA collectors/adapters | Source scope/freshness tests; unsupported tiers/types explicit; overlapping metrics and recommendations not double counted |
| 5 | Athena backend and normalized joins | SQLite/Athena contract parity on supported cases; decimal/overflow tests; monthly billing reconciliation with explained differences |
| 6 | Read-only MCP wrappers and AgentCore packaging | Fake-tool/model integration; identity isolation; bounds and failure handling; evidence capture; existing deterministic fallback retained |
| 7 | One live pilot investigation and savings follow-up | Explain a cost change; validate one opportunity; engineer reviews plan; measure later billing without mixing estimates and realised savings |

Reconciliation uses the same account scope, dates, currency and cost metric. Record absolute/relative differences and
their explanations; set the pilot's acceptance tolerance explicitly before signing off. No unexplained mismatch passes.
Exercise missing data, late revisions, permissions loss, stale metrics, conflicting recommendations and model failure.

Next code change: step 2, beginning with native CUR manifest/schema sample fixtures and an adapter that reuses the
existing all-or-nothing loader. It must not pretend a synthetic manifest is an AWS export.

Creating AWS resources, enabling exports, installing optional dependencies, live API reads and paid model evaluations
remain separate operational actions. This plan authorizes none of those actions by itself.

## 9. AWS references

- [Data Exports delivery and manifests](https://docs.aws.amazon.com/cur/latest/userguide/dataexports-export-delivery.html)
- [Cost Optimization Hub S3 Data Exports](https://aws.amazon.com/about-aws/whats-new/2024/06/aws-billing-cost-management-data-exports-cost-optimization-hub/)
- [Compute Optimizer exports](https://docs.aws.amazon.com/compute-optimizer/latest/ug/exporting-recommendations.html)
- [Compute Optimizer export restrictions](https://docs.aws.amazon.com/compute-optimizer/latest/ug/exporting-your-recommendations.html)
- [Storage Lens metrics exports](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage_lens_view_metrics_export.html)
- [Trusted Advisor API prerequisites](https://docs.aws.amazon.com/awssupport/latest/user/get-started-with-aws-trusted-advisor-api.html)
- [Savings Plans purchase recommendation API](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetSavingsPlansPurchaseRecommendation.html)
- [AgentCore Runtime and Gateway tutorial](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-cli-get-started.html)

Verify service schemas, supported regions, limits and IAM actions against current AWS documentation when implementing
each adapter. These links establish the integration direction, not a claim that the adapters are already tested.
