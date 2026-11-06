# Synthetic S3 pilot

All records are invented. Data is as of 3 November 2026, covering August to 2 November.
These are versioned CostLens test schemas, not native AWS export manifests. Model evaluations are not run by these commands.

## Current flow

```text
Deterministic fixtures -> typed Parquet -> five private S3 buckets
    -> fetch listed objects -> verify SHA-256 + schemas + totals
    -> local pilot SQLite -> existing CostLens query / optimization tools
```

One bucket each: CUR, Cost Optimization Hub, Compute Optimizer, Trusted Advisor, Savings Plans.
Storage Lens uses the MCP route by user preference; its generated local fixture is not uploaded to an export bucket.
The synthetic generator uses fixed existing fixture account/resource IDs to keep cross-source evidence correlated.
TA/SP overlap COH intentionally: their estimates must never be added as independent savings.

The publishing script creates new buckets only, blocks public access, enables AES256 server-side encryption and
versioning, labels them synthetic and uses conditional object creation. It never changes an existing bucket on rerun.
There is no automatic lifecycle cleanup configured. Keep object and bucket deletion as explicit operator actions.
The account being used for testing may have credits/free-tier benefits; the scripts make no promise that AWS use is free.

## Reproduce

Optional authoring dependencies: PyArrow and pytest. The app still has no new required runtime dependency.
The `parquet` optional dependency group declares PyArrow for generation and the pilot ingestion bridge.
Generated files, tools and local AWS destination records belong under ignored `var/`, not version control.

```powershell
python -m costlens.parquet_fixtures --out var/synthetic-parquet-NEW-RUN
./deploy/aws/publish-synthetic.ps1 -DataRoot var/synthetic-parquet-NEW-RUN -Profile costlens-demo -Region eu-central-1 -RunId NEW-RUN
./deploy/aws/fetch-synthetic.ps1 -Destinations var/synthetic-parquet-NEW-RUN/s3-destinations.json -OutputRoot var/downloaded-NEW-RUN -Profile costlens-demo
python -m costlens.ingest.parquet_snapshot --source var/downloaded-NEW-RUN --db var/s3-pilot.sqlite
python -m costlens serve --db var/s3-pilot.sqlite --port 8765
```

Replace NEW-RUN with a lowercase unique identifier; the publisher intentionally refuses to recreate existing buckets.
For existing buckets use the fetch script with their saved registrations. A future publishing revision should support
new prefixes in explicitly registered buckets without relaxing owner and no-overwrite checks.

The bridge validates all five sources before billing ingestion. Existing atomicity is per billing period or
optimization snapshot, not across the whole bundle. If a later import fails, already committed periods remain.
Use a dedicated pilot database and only expose it after a successful import. Current fixture history is a single
latest snapshot per period; revision/conflict behaviour is covered by the adapter tests, not separate uploaded revisions.

CUR, COH and Compute Optimizer feed existing calculations. TA and Savings Plans are validated context snapshots;
their new agent tool integration is pending. No new governance policies are inferred from these files.

## Production contract agreed with the user

The customer supplies existing production source bucket/prefix locations and read-only access. CostLens does not
create production exports, write to source buckets, or change source data. Configure source locations, catalog/database,
allowed tables, region, Athena workgroup, query limits and freshness policy outside model-controlled arguments.

Production will query through a scoped Athena workgroup and registered tables. Its results location (or managed
query-results configuration) must be approved separately from source read access. Glue/Lake Formation permissions,
KMS decrypt and cross-account permissions depend on the supplied environment. Do not widen scope to discover data.

Storage MCP must wrap actual supported APIs: configuration inspection is not usage telemetry. Verify the available
CloudWatch Storage Lens metrics and their prerequisites, or explicitly identify fallback S3 metrics; do not silently
label ordinary bucket metrics as Storage Lens. Missing/disabled telemetry returns insufficient evidence.

Pending steps: native schema adapters; Athena backend and scoped tables; Storage MCP wrapper; TA/SP tools; AgentCore
Runtime/Gateway deployment. The local ingestion bridge is an integration test, not the production Athena backend.

## Checks

`python -m pytest -q tests/test_parquet_fixtures.py` checks exact decimals, source markers, hashes, correlation,
no-overwrite behaviour, repeat ingestion, expected optimization totals and corruption before DB creation.
Use the existing ingestion/row tests alongside these for revisions, access failures and incomplete data.

## Verified pilot run: 5 November 2026

Profile: `costlens-demo`, region: `eu-central-1`. Bucket names are `costlens-synthetic-<source>-<account-id>`;
actual destinations are recorded locally under `var/synthetic-parquet-20261105-v1/s3-destinations.json`.
Run prefix: `runs/20261105-v1/`. Eight Parquet objects and five source manifests were published across five buckets.
Every data object was downloaded and checked against its SHA-256. A fresh read-only fetch retrieved manifests and
data again, and the CUR/COH/Compute Optimizer bridge ingested them into `var/s3-pilot-20261105.sqlite`.
The billing engine reproduced October's synthetic $18,561.93 total; optimization reproduced the $3,272.06 monthly
counted estimate. Neither is a real customer's spending or a realised saving.
