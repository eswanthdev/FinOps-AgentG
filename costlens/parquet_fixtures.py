"""Deterministic, wholly synthetic Parquet evidence. No AWS API calls.

Schemas are CostLens synthetic contracts, not promises of native AWS export parity.
PyArrow is an optional authoring dependency; money is encoded as decimal128.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path

from . import fixtures
from .ingest.cur2 import parse_row

AS_OF = "2026-11-03T00:00:00Z"
SOURCES = ("cur", "cost-optimization-hub", "compute-optimizer", "storage-lens",
           "trusted-advisor", "savings-plans")
_CUR_DECIMALS = {"line_item_usage_amount", "line_item_unblended_cost", "pricing_public_on_demand_cost",
                 "savings_plan_savings_plan_effective_cost", "savings_plan_total_commitment_to_date",
                 "savings_plan_used_commitment"}
_CO_DECIMALS = {"currentOnDemandPrice", "utilizationMetrics_CPU_MAXIMUM", "utilizationMetrics_MEMORY_MAXIMUM",
                "recommendationOptions_1_performanceRisk", "recommendationOptions_1_estimatedMonthlySavings_value"}
_Q = Decimal("0.0000000001")


def arrow_modules():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Parquet authoring needs the optional pyarrow package") from exc
    return pa, pq


def _decimal_columns(source: str, row: dict) -> set[str]:
    if source == "cur":
        return _CUR_DECIMALS
    if source == "cost-optimization-hub":
        return {k for k in row if k.startswith("estimated_monthly_")}
    if source == "compute-optimizer":
        return _CO_DECIMALS
    if source == "savings-plans":
        return {"hourly_commitment", "estimated_monthly_savings"}
    return set()


def _table(source: str, rows: list[dict]):
    pa, _ = arrow_modules()
    decimals = _decimal_columns(source, rows[0])
    converted = []
    for raw in rows:
        row = dict(raw)
        for key in decimals:
            value = row.get(key)
            row[key] = Decimal(str(value)).quantize(_Q, rounding=ROUND_HALF_EVEN) if value not in (None, "") else None
        row.update(synthetic_data=True, fixture_schema_version="costlens-synthetic-parquet-v1", as_of=AS_OF)
        converted.append(row)
    fields = []
    for key, value in converted[0].items():
        dtype = pa.decimal128(38, 10) if key in decimals else (
            pa.bool_() if isinstance(value, bool) else pa.int64() if isinstance(value, int) else pa.string())
        fields.append(pa.field(key, dtype))
    schema = pa.schema(fields, metadata={b"synthetic_data": b"true", b"source": source.encode(),
                                        b"schema_version": b"costlens-synthetic-parquet-v1"})
    return pa.Table.from_pylist(converted, schema=schema)


def _extra_sources():
    # Correlate the storage metrics with the existing S3 fixture resource IDs.
    storage = []
    for resource in fixtures.build_resources():
        if resource.service != "AmazonS3":
            continue
        for metric, value in (("StorageBytes", 500 * 1024**3), ("NonCurrentVersionStorageBytes", 100 * 1024**3),
                              ("IncompleteMultipartUploadStorageBytes", 20 * 1024**3)):
            storage.append({"account_id": resource.account, "region": resource.region,
                            "bucket": resource.resource_id, "observation_date": "2026-11-02",
                            "aggregation_level": "bucket", "metric": metric, "value": value,
                            "unit": "bytes", "scenario": "Planted storage cleanup candidate; validate retention and dependencies"})
    advisor = []
    for rec in fixtures.coh_rows():
        if rec["action_type"] == "Stop":
            advisor.append({"recommendation_id": "synthetic-ta-" + rec["recommendation_id"],
                            "linked_coh_recommendation_id": rec["recommendation_id"],
                            "account_id": rec["account_id"], "region": rec["region"],
                            "resource_arn": rec["resource_arn"], "category": "cost_optimizing",
                            "status": "warning", "check_name": "Synthetic low-utilization resource",
                            "overlap_note": "Same resource as COH; not an additional saving"})
    plans = []
    for rec in fixtures.coh_rows():
        if rec["action_type"] != "PurchaseSavingsPlans":
            continue
        details = json.loads(rec["recommended_resource_details"])
        plans.append({"recommendation_id": "synthetic-sp-" + rec["recommendation_id"],
                      "linked_coh_recommendation_id": rec["recommendation_id"], "account_id": rec["account_id"],
                      "scope": "LINKED", "lookback_days": 30, "term": str(details.get("term", "ONE_YEAR")),
                      "payment_option": str(details.get("paymentOption", "NO_UPFRONT")),
                      "hourly_commitment": str(details.get("hourlyCommitment", "1")),
                      "estimated_monthly_savings": rec["estimated_monthly_savings_after_discount"],
                      "currency_code": "USD", "status": "proposal_only",
                      "overlap_note": "Recalculate after rightsizing; do not add to COH savings"})
    return {"storage-lens": storage, "trusted-advisor": advisor, "savings-plans": plans}


def generate(out: str | Path) -> dict:
    _, pq = arrow_modules()
    root = Path(out)
    if root.exists() and any(root.iterdir()):
        raise ValueError("output folder must be empty; existing evidence is never overwritten")
    root.mkdir(parents=True, exist_ok=True)
    datasets = []
    for period, through, revised in (("2026-08", date(2026, 8, 31), False),
                                     ("2026-09", date(2026, 9, 30), True),
                                     ("2026-10", date(2026, 10, 31), False),
                                     ("2026-11", date(2026, 11, 2), False)):
        rows = fixtures.rows_for_period(period, through, revision=revised)
        for row in rows:
            row["line_item_currency_code"] = "USD"
        total = sum(parse_row(row, period).unblended_nanos for row in rows)
        datasets.append(("cur", f"billing_period={period}/part-00001.parquet", rows,
                         {"billing_period": period, "unblended_total_nanos": str(total),
                          "data_through": through.isoformat(), "billing_finality": "provisional"}))
    for source, rows in {"cost-optimization-hub": fixtures.coh_rows(),
                         "compute-optimizer": fixtures.co_rows(), **_extra_sources()}.items():
        datasets.append((source, "snapshot_date=2026-11-02/part-00001.parquet", rows, {}))
    manifests = {}
    for source, relative, rows, extra in datasets:
        if not rows:
            raise ValueError(f"source {source} has no fixture rows")
        table = _table(source, rows)
        key = f"{source}/{relative}"
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path, compression="snappy")
        actual = pq.ParquetFile(path).read()
        if not actual.equals(table, check_metadata=True):
            raise ValueError(f"Parquet round-trip failed: {key}")
        entry = {"key": relative, "row_count": len(rows), "bytes": path.stat().st_size,
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **extra}
        manifest = manifests.setdefault(source, {
            "schema_version": "costlens-synthetic-parquet-v1", "synthetic_data": True, "source": source,
            "as_of": AS_OF, "reference_date": fixtures.REFERENCE_DATE.isoformat(),
            "warning": "Entirely invented data. Not a native AWS manifest or evidence of real savings.",
            "files": [],
        })
        manifest["files"].append(entry)
    for source, manifest in manifests.items():
        (root / source / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    summary = {"synthetic_data": True, "schema_version": "costlens-synthetic-parquet-v1",
               "sources": list(manifests), "parquet_files": len(datasets),
               "rows": sum(entry["row_count"] for m in manifests.values() for entry in m["files"])}
    (root / "catalog.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.out), indent=2))


if __name__ == "__main__":
    main()
