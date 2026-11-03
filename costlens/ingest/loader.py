"""Idempotent ingestion of CUR 2.0 deliveries.

Outcomes per delivery (stored in deliveries.status):
  active             new data for a billing period, now used by queries
  superseded         was active, replaced by a newer revision of the same period
  duplicate          same execution id and identical content already ingested; skipped
  stale              older than the active delivery for the period; skipped
  rejected_incomplete  a listed file is missing, truncated, or row count differs
  rejected_invalid   manifest or rows fail validation (nothing loaded)
  rejected_conflict  same execution id seen before with different content
  rejected_access    a file could not be read because of permissions

A delivery is loaded all-or-nothing in one transaction. Nothing is ever added on
top of an existing period: a revision replaces the period's rows, so totals are
never double counted.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass

from ..money import nanos_to_cents_str
from ..store import transaction, utc_now
from .cur2 import CurRow, RowError, parse_row
from .sources import DeliveryRef, ExportSource, FileAccessDenied

SCHEMA_VERSION = "cur2-subset-v1"
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_ROWS_PER_DELIVERY = 2_000_000


@dataclass
class Outcome:
    status: str
    billing_period: str
    execution_id: str
    detail: str
    source_uri: str
    row_count: int = 0
    unblended_total: str = "0.00"

    def to_dict(self) -> dict:
        return asdict(self)


class _Reject(Exception):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _validate_manifest(m: dict) -> dict:
    if not isinstance(m, dict):
        raise _Reject("rejected_invalid", "manifest is not a JSON object")
    if m.get("schemaVersion") != SCHEMA_VERSION:
        raise _Reject("rejected_invalid", f"unsupported schemaVersion {m.get('schemaVersion')!r}")
    for field in ("exportName", "executionId", "billingPeriod", "exportTimestamp", "dataFiles"):
        if field not in m:
            raise _Reject("rejected_invalid", f"manifest missing {field}")
    if not _PERIOD_RE.match(str(m["billingPeriod"])):
        raise _Reject("rejected_invalid", "billingPeriod must be YYYY-MM")
    for field in ("exportName", "executionId"):
        if not _SAFE_ID_RE.match(str(m[field])):
            raise _Reject("rejected_invalid", f"{field} has unexpected characters")
    files = m["dataFiles"]
    if not isinstance(files, list) or not files:
        raise _Reject("rejected_invalid", "dataFiles must be a non-empty list")
    for f in files:
        if not isinstance(f, dict) or not {"key", "sha256", "rowCount"} <= f.keys():
            raise _Reject("rejected_invalid", "each dataFiles entry needs key, sha256, rowCount")
    return m


def _decode_csv(data: bytes, key: str) -> list[dict[str, str]]:
    if key.endswith(".gz"):
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise _Reject("rejected_incomplete", f"{key} is not a complete gzip file") from exc
    text = data.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _read_delivery(source: ExportSource, ref: DeliveryRef) -> tuple[dict, list[CurRow], str]:
    try:
        manifest = _validate_manifest(source.read_manifest(ref))
    except FileAccessDenied as exc:
        raise _Reject("rejected_access", str(exc)) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _Reject("rejected_invalid", "manifest is not valid JSON") from exc

    period = manifest["billingPeriod"]
    rows: list[CurRow] = []
    hasher = hashlib.sha256(manifest["executionId"].encode())
    errors: list[str] = []
    for f in sorted(manifest["dataFiles"], key=lambda x: str(x["key"])):
        key = str(f["key"])
        try:
            data = source.read_data_file(ref, key)
        except FileAccessDenied as exc:
            raise _Reject("rejected_access", str(exc)) from exc
        if data is None:
            raise _Reject("rejected_incomplete", f"listed data file {key} is missing")
        digest = hashlib.sha256(data).hexdigest()
        if digest != f["sha256"]:
            raise _Reject("rejected_incomplete", f"{key} checksum does not match manifest (truncated or changed)")
        raw_rows = _decode_csv(data, key)
        if len(raw_rows) != int(f["rowCount"]):
            raise _Reject("rejected_incomplete", f"{key} has {len(raw_rows)} rows, manifest says {f['rowCount']}")
        hasher.update(key.encode())
        hasher.update(digest.encode())
        for i, raw in enumerate(raw_rows, start=2):
            try:
                rows.append(parse_row(raw, period))
            except RowError as exc:
                if len(errors) < 5:
                    errors.append(f"{key} line {i}: {exc}")
        if len(rows) > MAX_ROWS_PER_DELIVERY:
            raise _Reject("rejected_invalid", "delivery exceeds the row limit for local mode")
    if errors:
        raise _Reject("rejected_invalid", "; ".join(errors))

    seen: set[str] = set()
    for r in rows:
        if r.line_item_id in seen:
            raise _Reject("rejected_invalid", f"duplicate identity_line_item_id {r.line_item_id} inside delivery")
        seen.add(r.line_item_id)
    return manifest, rows, hasher.hexdigest()


def _record(conn, ref: DeliveryRef, manifest: dict | None, status: str, detail: str, *,
            fingerprint: str = "", rows: list[CurRow] | None = None) -> int:
    m = manifest or {}
    rows = rows or []
    total = sum(r.unblended_nanos for r in rows)
    data_through = max((r.usage_date for r in rows), default=None)
    cur = conn.execute(
        """INSERT INTO deliveries(source_uri, export_name, billing_period, execution_id, export_timestamp,
               fingerprint, status, detail, row_count, unblended_total_nanos, period_complete, data_through, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ref.uri,
            str(m.get("exportName", "")),
            str(m.get("billingPeriod", "")),
            str(m.get("executionId", "")),
            str(m.get("exportTimestamp", "")),
            fingerprint,
            status,
            detail,
            len(rows),
            total,
            1 if m.get("periodComplete") else 0,
            data_through,
            utc_now(),
        ),
    )
    return cur.lastrowid


def _audit(conn, event_type: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events(ts, event_type, detail) VALUES (?,?,?)",
        (utc_now(), event_type, json.dumps(detail, sort_keys=True)),
    )


def ingest_delivery(conn, source: ExportSource, ref: DeliveryRef) -> Outcome:
    manifest: dict | None = None
    try:
        manifest, rows, fingerprint = _read_delivery(source, ref)
    except _Reject as rej:
        try:
            manifest = source.read_manifest(ref)
            if not isinstance(manifest, dict):
                manifest = None
        except Exception:  # noqa: BLE001 - manifest itself may be the problem
            manifest = None
        with transaction(conn):
            _record(conn, ref, manifest, rej.status, rej.detail)
            _audit(conn, "ingest." + rej.status, {"uri": ref.uri, "detail": rej.detail})
        m = manifest or {}
        return Outcome(rej.status, str(m.get("billingPeriod", "")), str(m.get("executionId", "")), rej.detail, ref.uri)

    export, period, exec_id = manifest["exportName"], manifest["billingPeriod"], manifest["executionId"]
    ts = str(manifest["exportTimestamp"])
    total_str = nanos_to_cents_str(sum(r.unblended_nanos for r in rows))

    with transaction(conn):
        prior = conn.execute(
            "SELECT fingerprint FROM deliveries WHERE export_name=? AND execution_id=? AND status IN ('active','superseded','stale')",
            (export, exec_id),
        ).fetchone()
        if prior:
            if prior["fingerprint"] == fingerprint:
                status, detail = "duplicate", "identical delivery already ingested; skipped"
            else:
                status, detail = "rejected_conflict", "execution id seen before with different content; not loaded"
            _record(conn, ref, manifest, status, detail, fingerprint=fingerprint)
            _audit(conn, "ingest." + status, {"uri": ref.uri, "period": period, "execution_id": exec_id})
            return Outcome(status, period, exec_id, detail, ref.uri, len(rows), total_str)

        active = conn.execute(
            "SELECT id, export_timestamp, unblended_total_nanos, execution_id FROM deliveries "
            "WHERE export_name=? AND billing_period=? AND status='active'",
            (export, period),
        ).fetchone()
        if active and active["export_timestamp"] >= ts:
            status = "stale"
            detail = f"older than active delivery {active['execution_id']}; skipped"
            _record(conn, ref, manifest, status, detail, fingerprint=fingerprint, rows=rows)
            conn.execute("UPDATE deliveries SET row_count=0 WHERE id=last_insert_rowid()")
            _audit(conn, "ingest.stale", {"uri": ref.uri, "period": period, "execution_id": exec_id})
            return Outcome(status, period, exec_id, detail, ref.uri, 0, total_str)

        if active:
            conn.execute("DELETE FROM line_items WHERE billing_period=?", (period,))
            conn.execute("UPDATE deliveries SET status='superseded' WHERE id=?", (active["id"],))
            delta = sum(r.unblended_nanos for r in rows) - active["unblended_total_nanos"]
            sign = "+" if delta >= 0 else "-"
            detail = (
                f"revision of {period}: replaced delivery {active['execution_id']}; "
                f"unblended total changed by {sign}${nanos_to_cents_str(abs(delta))}"
            )
        else:
            detail = f"first delivery for {period}"

        delivery_id = _record(conn, ref, manifest, "active", detail, fingerprint=fingerprint, rows=rows)
        conn.executemany(
            """INSERT INTO line_items(delivery_id, billing_period, line_item_id, payer_account_id, account_id,
                   line_item_type, usage_date, service, usage_type, operation, resource_id, region, usage_amount,
                   unblended_nanos, on_demand_nanos, amortized_nanos, resource_tags)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (delivery_id, r.billing_period, r.line_item_id, r.payer_account_id, r.account_id, r.line_item_type,
                 r.usage_date, r.service, r.usage_type, r.operation, r.resource_id, r.region, r.usage_amount,
                 r.unblended_nanos, r.on_demand_nanos, r.amortized_nanos, r.tags_json)
                for r in rows
            ],
        )
        for acct_id, name in {(r.account_id, r.account_name) for r in rows if r.account_name}:
            conn.execute(
                "INSERT INTO accounts(account_id, account_name) VALUES (?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET account_name=excluded.account_name",
                (acct_id, name),
            )
        _audit(conn, "ingest.active", {"uri": ref.uri, "period": period, "execution_id": exec_id, "rows": len(rows)})
    return Outcome("active", period, exec_id, detail, ref.uri, len(rows), total_str)


def ingest_source(conn, source: ExportSource) -> list[Outcome]:
    return [ingest_delivery(conn, source, ref) for ref in source.list_deliveries()]
