"""Local AWS CUR CSV adapter. Native manifest discovery is not implemented yet.

The caller supplies a complete file list and verified delivery metadata. The
adapter builds our INTERNAL manifest; this is never presented as an AWS schema.
No AWS requests or third-party packages are used.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from .cur2 import REQUIRED_COLUMNS, RowError, parse_row
from .loader import MAX_ROWS_PER_DELIVERY, ingest_delivery
from .sources import DeliveryRef, FileAccessDenied

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DELIVERY_BYTES = 128 * 1024 * 1024
_OPTIONAL = {
    "line_item_usage_account_name": "", "line_item_resource_id": "",
    "line_item_operation": "", "pricing_public_on_demand_cost": "",
    "resource_tags": "{}",
    "savings_plan_savings_plan_effective_cost": "",
    "savings_plan_total_commitment_to_date": "", "savings_plan_used_commitment": "",
}


def normalize_row(raw: dict[str, str], period: str) -> dict[str, str]:
    """Map unaliased CUR columns to the current engine's supported USD subset."""
    if raw.get("line_item_currency_code", "").strip() != "USD":
        raise RowError("line_item_currency_code must explicitly be USD")
    row = {**_OPTIONAL, **raw}
    if "product_region_code" not in raw:
        try:
            product = json.loads(raw.get("product") or "{}")
        except (ValueError, TypeError) as exc:
            raise RowError("product must be a JSON map") from exc
        if not isinstance(product, dict):
            raise RowError("product must be a JSON map")
        region = product.get("region_code", "")
        if not isinstance(region, str):
            raise RowError("product region_code must be a string")
        row["product_region_code"] = region
    parse_row(row, period)  # Missing commitment fields must not become zero savings.
    return {key: row[key] for key in REQUIRED_COLUMNS}


class LocalAwsCurDelivery:
    """An explicitly scoped local CSV snapshot, suitable for ingest_delivery.

    Preparation fails before any DB mutation. It does not establish authenticity,
    file completeness against an AWS manifest, or final billing status.
    """

    name = "aws-cur-local-csv"

    def __init__(self, root: str | Path, *, files: list[str], export_name: str,
                 execution_id: str, billing_period: str, generated_at: str):
        self.root = Path(root).resolve()
        self.ref = DeliveryRef(str(self.root))
        if not files or len(files) > 100 or len(set(files)) != len(files):
            raise ValueError("supply 1-100 distinct files from one complete delivery")
        timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("generated_at needs an explicit timezone")
        self.payloads: dict[str, bytes] = {}
        entries = []
        total_bytes = total_rows = 0
        for index, key in enumerate(files):
            if not isinstance(key, str) or not key or Path(key).is_absolute():
                raise FileAccessDenied("file keys must be relative to the snapshot root")
            path = (self.root / key).resolve()
            if self.root not in path.parents:
                raise FileAccessDenied("file key escapes the snapshot root")
            if not key.endswith((".csv", ".csv.gz")):
                raise ValueError("only CSV and gzip CSV are supported; Parquet is not implemented")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("file exceeds local adapter byte limit")
            if key.endswith(".gz"):
                with gzip.open(path, "rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
            else:
                data = path.read_bytes()
            total_bytes += len(data)
            if len(data) > MAX_FILE_BYTES or total_bytes > MAX_DELIVERY_BYTES:
                raise ValueError("decoded data exceeds local adapter byte limit")
            reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), strict=True)
            headers = reader.fieldnames
            if not headers or len(set(headers)) != len(headers):
                raise RowError("CSV needs distinct named columns")
            required = set(REQUIRED_COLUMNS) - set(_OPTIONAL) - {"product_region_code"}
            required.add("line_item_currency_code")
            if not required <= set(headers):
                raise RowError("missing columns: " + ", ".join(sorted(required - set(headers))))
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=REQUIRED_COLUMNS)
            writer.writeheader()
            count = 0
            for raw in reader:
                if None in raw or any(v is None for v in raw.values()):
                    raise RowError("CSV row width differs from header")
                writer.writerow(normalize_row(raw, billing_period))
                count += 1
                total_rows += 1
                if total_rows > MAX_ROWS_PER_DELIVERY:
                    raise ValueError("delivery exceeds local adapter row limit")
            payload = output.getvalue().encode("utf-8")
            internal_key = f"part-{index:05d}.csv"
            self.payloads[internal_key] = payload
            entries.append({"key": internal_key, "sha256": hashlib.sha256(payload).hexdigest(), "rowCount": count})
        if not total_rows:
            raise RowError("empty snapshot cannot replace a billing period")
        self.manifest = {
            "schemaVersion": "cur2-subset-v1", "exportName": export_name,
            "executionId": execution_id, "billingPeriod": billing_period,
            "exportTimestamp": timestamp.astimezone(timezone.utc).isoformat(),
            "periodComplete": False, "dataFiles": entries,
        }

    def read_manifest(self, ref: DeliveryRef) -> dict:
        if ref != self.ref:
            raise FileAccessDenied("unknown snapshot")
        return self.manifest

    def read_data_file(self, ref: DeliveryRef, key: str) -> bytes | None:
        if ref != self.ref:
            raise FileAccessDenied("unknown snapshot")
        return self.payloads.get(key)

    def ingest(self, conn):
        # Existing storage is one tenant/payer/export per database. Enforce this
        # until a scoped production store replaces period-wide deletion.
        payers = set()
        for payload in self.payloads.values():
            payers.update(r["bill_payer_account_id"] for r in csv.DictReader(io.StringIO(payload.decode())))
        existing = {r[0] for r in conn.execute("SELECT DISTINCT payer_account_id FROM line_items")}
        exports = {r[0] for r in conn.execute("SELECT DISTINCT export_name FROM deliveries WHERE status='active'")}
        if len(payers) != 1 or (existing and existing != payers):
            raise ValueError("use a separate database for each payer")
        if exports and exports != {self.manifest["exportName"]}:
            raise ValueError("use a separate database for each export")
        return ingest_delivery(conn, self, self.ref)
