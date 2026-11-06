# Local AWS CUR CSV ingestion

Status: first adapter slice. CSV and gzip CSV ingestion is implemented. Native AWS manifest discovery,
Parquet, S3 access and real-customer reconciliation are not implemented or verified.
Tests use constructed AWS-shaped rows, not captured customer exports.

Use a dedicated database for one payer and one export. Do not mix real snapshots with the demo database.
Download one complete delivery and preserve its file list, source generation timestamp and execution identity.
The caller currently supplies those values; the adapter cannot establish manifest completeness or authenticity.

```powershell
python -m costlens ingest-aws-cur --source ./downloaded-cur --file part-00001.csv.gz --file part-00002.csv.gz --db ./var/customer.sqlite --export-name customer-cur --execution-id execution-001 --billing-period 2026-10 --generated-at 2026-11-02T00:00:00Z
```

The example filenames and identifiers are placeholders. Use the exact relative files and verified metadata
from your delivery. Never recursively ingest every CSV under an export prefix: revisions can overlap.
The command only ingests billing and prints the outcome. It does not send budget emails or call AWS.

## Supported input

- Unaliased CUR 2.0 snake-case CSV columns required by `costlens/ingest/cur2.py`.
- Explicit `line_item_currency_code=USD` on every row. Missing/other currencies are rejected.
- `product_region_code`, or a `product` JSON map containing `region_code`.
- Optional account name, operation, resource ID, public on-demand cost and resource tags. Missing resource ID
  remains unknown. Missing tags become an empty map. Tags, when supplied, must be a JSON string-to-string map.
- Savings Plan fields can be absent on unrelated charges. Charges that need commitment evidence are rejected
  if that evidence is missing. Existing unsupported amortization types retain insufficient-data behaviour.
- Negative credits and refunds retain their sign. Monetary arithmetic uses existing integer nano-USD rules.

Preparation reads and validates every selected file before touching billing data. All rows must belong to the
selected period and one payer. Duplicate row identities are rejected by the existing loader. The adapter builds
an internal `cur2-subset-v1` delivery in memory; that is not an AWS manifest format.

Exact repeat deliveries are duplicates; changed data under the same execution ID conflicts; a later delivery
replaces the period atomically through the existing loader. Every imported period is provisional. No age rule
or manually provided generation timestamp is treated as proof of final billing.

Local limits: 100 files, 64 MiB per compressed/raw file and per decoded file, 128 MiB total decoded source data,
and the existing two-million-row ceiling. Normalization adds memory overhead; this adapter is for bounded pilot
snapshots, not a streaming production ingestion service.

## Verification

```powershell
python -m unittest -v tests.test_aws_cur_stdlib
```

The checks need only Python's standard library. The normal pytest suite also discovers these unittest tests.
Run the existing ingestion/row suite when pytest is available. No dependency is installed by this feature.

## Next adapter work

Obtain a sanitized native manifest plus matching data headers. Confirm its exact schema and execution timestamp
semantics before writing discovery/mapping logic. Retain the manifest, raw object hashes and source references;
then test missing chunks and overwritten generations. Add Parquet behind an explicit optional dependency or the
planned Athena path. Do not claim full real AWS export support before testing captured export samples.

AWS references: [delivery layout](https://docs.aws.amazon.com/cur/latest/userguide/dataexports-export-delivery.html)
and [CUR 2.0 schema migration](https://docs.aws.amazon.com/cur/latest/userguide/dataexports-migrate.html).
