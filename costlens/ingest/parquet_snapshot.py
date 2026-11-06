"""Validate synthetic Parquet snapshots and feed the existing FinOps engine.

Only the labelled CostLens synthetic contract is supported. This is not a native
AWS export adapter. Callers can stage these files from S3 before ingestion.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from .. import fixtures
from ..parquet_fixtures import arrow_modules
from ..store import connect, set_meta
from .aws_cur import LocalAwsCurDelivery
from .optimization import ingest_optimization, parse_coh_row, parse_co_row
from .sources import DeliveryRef

SOURCE_NAMES = ("cur", "cost-optimization-hub", "compute-optimizer", "trusted-advisor", "savings-plans")
_METADATA = {"synthetic_data", "fixture_schema_version", "as_of"}


def read_source(root: Path, source: str) -> tuple[dict, list[tuple[dict, list[dict]]]]:
    _, pq = arrow_modules()
    base = (root / source).resolve()
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8-sig"))
    if (manifest.get("synthetic_data") is not True or manifest.get("source") != source
            or manifest.get("schema_version") != "costlens-synthetic-parquet-v1"):
        raise ValueError("not a supported synthetic snapshot")
    files = manifest.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 100:
        raise ValueError("snapshot must list 1-100 files")
    seen = set()
    batches = []
    for entry in files:
        key = entry["key"]
        path = (base / key).resolve()
        if base not in path.parents or path in seen:
            raise ValueError("unsafe or duplicate snapshot key")
        seen.add(path)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("snapshot exceeds pilot file limit")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("snapshot checksum mismatch")
        parquet = pq.ParquetFile(path)
        if parquet.metadata.num_rows != entry["row_count"] or not 0 < parquet.metadata.num_rows <= 2_000_000:
            raise ValueError("snapshot row count mismatch")
        table = parquet.read()
        if (table.schema.metadata or {}).get(b"synthetic_data") != b"true":
            raise ValueError("Parquet synthetic marker missing")
        rows = table.to_pylist()
        if not all(r.get("synthetic_data") is True for r in rows):
            raise ValueError("row synthetic marker missing")
        batches.append((entry, [{k: (format(v.normalize(), "f") if isinstance(v, Decimal)
                                    else str(v) if v is not None else "") for k, v in row.items()
                                  if k not in _METADATA} for row in rows]))
    return manifest, batches


def ingest(root: str | Path, db: str | Path) -> dict:
    root = Path(root)
    # Read every source before loading any billing data. Missing sources are errors.
    sources = {source: read_source(root, source) for source in SOURCE_NAMES}
    from .cur2 import parse_row
    for entry, rows in sources["cur"][1]:
        total = sum(parse_row(row, entry["billing_period"]).unblended_nanos for row in rows)
        if str(total) != entry["unblended_total_nanos"]:
            raise ValueError("CUR reconciliation failed")
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="costlens-parquet-") as temporary:
        stage = Path(temporary)
        deliveries = []
        for entry, rows in sources["cur"][1]:
            period = entry["billing_period"]
            key = f"{period}.csv"
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            (stage / key).write_text(output.getvalue(), encoding="utf-8")
            delivery = LocalAwsCurDelivery(stage, files=[key], export_name="synthetic-parquet-cur",
                execution_id="parquet-" + entry["sha256"][:24], billing_period=period,
                generated_at=sources["cur"][0]["as_of"])
            delivery.ref = DeliveryRef(str((root / "cur" / entry["key"]).resolve()))
            deliveries.append(delivery)
        coh = [r for _, rows in sources["cost-optimization-hub"][1] for r in rows]
        co = [r for _, rows in sources["compute-optimizer"][1] for r in rows]
        for row in coh:
            parse_coh_row(row)
        for row in co:
            parse_co_row(row)
        fixtures.write_optimization_snapshot(stage / "optimization", "snapshot", "parquet-opt-" + hashlib.sha256(
            json.dumps([coh, co], sort_keys=True).encode()).hexdigest()[:24],
            sources["cost-optimization-hub"][0]["as_of"], coh, co)
        conn = connect(db)
        try:
            for delivery in deliveries:
                outcome = delivery.ingest(conn)
                outcomes.append(outcome.to_dict())
                if outcome.status not in ("active", "duplicate", "stale"):
                    raise ValueError(f"CUR delivery failed: {outcome.detail}")
            optimization = ingest_optimization(conn, stage / "optimization")
            outcomes.extend(o.to_dict() for o in optimization)
            if any(o.status not in ("active", "duplicate", "stale") for o in optimization):
                raise ValueError("optimization snapshot validation failed: " + "; ".join(o.detail for o in optimization))
            set_meta(conn, "data_mode", "synthetic")
            set_meta(conn, "reference_date", fixtures.REFERENCE_DATE.isoformat())
            set_meta(conn, "expected_start_period", fixtures.EXPECTED_START_PERIOD)
            set_meta(conn, "synthetic_parquet_root", str(root.resolve()))
        finally:
            conn.close()
    return {"synthetic_data": True, "outcomes": outcomes,
            "staged_context_sources": ["trusted-advisor", "savings-plans"],
            "note": "TA/SP are validated evidence snapshots; new agent tools for them are pending."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(json.dumps(ingest(args.source, args.db), indent=2))


if __name__ == "__main__":
    main()
