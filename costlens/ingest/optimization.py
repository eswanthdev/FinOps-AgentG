"""Idempotent ingestion of optimization snapshots.

A snapshot is one folder holding a manifest plus a Cost Optimization Hub export
and/or a Compute Optimizer EC2 export. Column names follow the real AWS exports
but are a subset, and the files are synthetic. They have not been tested against
real exports (see docs/DATA_CONTRACTS.md).

Recommendations are a point-in-time view, not a ledger: a newer snapshot replaces
the previous one completely, so nothing is ever carried over or added twice.
Outcomes match CUR deliveries (active, superseded, duplicate, stale, rejected_*).
A snapshot is loaded all-or-nothing in one transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..money import MoneyError, parse_usd_to_nanos
from ..store import transaction, utc_now
from .loader import Outcome, _audit, _decode_csv, _Reject
from .sources import DeliveryRef, FileAccessDenied, LocalDirectorySource

OPT_SCHEMA_VERSION = "opt-subset-v1"
MAX_ROWS_PER_FILE = 5_000
TABLES = ("coh", "compute_optimizer_ec2")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ACCOUNT_RE = re.compile(r"^\d{12}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PCT_RE = re.compile(r"^\d{1,3}(\.\d{1,4})?$")

COH_COLUMNS = (
    "recommendation_id", "account_id", "region", "resource_arn", "action_type", "currency_code",
    "current_resource_type", "current_resource_summary", "recommended_resource_type",
    "recommended_resource_summary", "recommended_resource_details", "recommendation_source",
    "estimated_monthly_cost_before_discount", "estimated_monthly_cost_after_discount",
    "estimated_monthly_savings_before_discount", "estimated_monthly_savings_after_discount",
    "implementation_effort", "restart_needed", "rollback_possible",
    "recommendation_lookback_period_in_days", "last_refresh_timestamp",
)
CO_COLUMNS = (
    "accountId", "instanceArn", "finding", "currentInstanceType", "currentOnDemandPrice",
    "utilizationMetrics_CPU_MAXIMUM", "utilizationMetrics_MEMORY_MAXIMUM",
    "recommendationOptions_1_instanceType", "recommendationOptions_1_performanceRisk",
    "recommendationOptions_1_estimatedMonthlySavings_value",
    "recommendationOptions_1_estimatedMonthlySavings_currency",
    "recommendationOptions_1_migrationEffort", "lookbackPeriodInDays", "lastRefreshTimestamp",
)
COH_ACTIONS = ("Rightsize", "Stop", "Delete", "Upgrade", "MigrateToGraviton", "ScaleIn",
               "PurchaseSavingsPlans", "PurchaseReservedInstances")
EFFORTS = ("VeryLow", "Low", "Medium", "High", "VeryHigh")
CO_FINDINGS = ("OVER_PROVISIONED", "UNDER_PROVISIONED", "OPTIMIZED")


class _RowError(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    manifest: dict
    coh: list[dict]
    co: list[dict]
    fingerprint: str


def resource_id_from_arn(arn: str) -> str:
    return re.split(r"[/:]", arn)[-1] if arn else ""


def _money(raw: dict, column: str) -> int:
    try:
        nanos = parse_usd_to_nanos(raw.get(column))
    except MoneyError as exc:
        raise _RowError(f"{column}: {exc}") from exc
    if nanos < 0:
        raise _RowError(f"{column}: must not be negative")
    return nanos


def _text(raw: dict, column: str, limit: int = 300, *, required: bool = True) -> str:
    value = (raw.get(column) or "").strip()
    if required and not value:
        raise _RowError(f"{column}: is empty")
    if len(value) > limit:
        raise _RowError(f"{column}: longer than {limit} characters")
    return value


def _choice(raw: dict, column: str, allowed: tuple[str, ...]) -> str:
    value = (raw.get(column) or "").strip()
    if value not in allowed:
        raise _RowError(f"{column}: unexpected value {value!r}")
    return value


def _flag(raw: dict, column: str) -> int:
    value = (raw.get(column) or "").strip().lower()
    if value not in ("true", "false"):
        raise _RowError(f"{column}: must be true or false")
    return 1 if value == "true" else 0


def _lookback(raw: dict, column: str) -> int:
    value = (raw.get(column) or "").strip()
    if not value.isdigit() or not 0 < int(value) <= 400:
        raise _RowError(f"{column}: must be a whole number of days from 1 to 400")
    return int(value)


def _timestamp(raw: dict, column: str) -> str:
    value = (raw.get(column) or "").strip()
    if not _TS_RE.match(value):
        raise _RowError(f"{column}: must be YYYY-MM-DDTHH:MM:SSZ")
    return value


def _account(raw: dict, column: str) -> str:
    value = (raw.get(column) or "").strip()
    if not _ACCOUNT_RE.match(value):
        raise _RowError(f"{column}: must be 12 digits")
    return value


def _percent(raw: dict, column: str) -> str:
    value = (raw.get(column) or "").strip()
    if value and not _PCT_RE.match(value):
        raise _RowError(f"{column}: must be a number or empty")
    return value


def parse_coh_row(raw: dict) -> dict:
    if (raw.get("currency_code") or "").strip() != "USD":
        raise _RowError("currency_code: only USD is supported")
    details = _text(raw, "recommended_resource_details", 1000, required=False) or "{}"
    try:
        if not isinstance(json.loads(details), dict):
            raise ValueError
    except ValueError as exc:
        raise _RowError("recommended_resource_details: must be a JSON object") from exc
    arn = _text(raw, "resource_arn", 200, required=False)  # 200 = CostQuery filter value limit
    return {
        "recommendation_id": _text(raw, "recommendation_id", 128),
        "account_id": _account(raw, "account_id"),
        "region": _text(raw, "region", 40),
        "resource_arn": arn,
        "resource_id": resource_id_from_arn(arn),
        "action_type": _choice(raw, "action_type", COH_ACTIONS),
        "current_resource_type": _text(raw, "current_resource_type", 60),
        "current_summary": _text(raw, "current_resource_summary", required=False),
        "recommended_resource_type": _text(raw, "recommended_resource_type", 60),
        "recommended_summary": _text(raw, "recommended_resource_summary", required=False),
        "recommended_details": details,
        "source": _text(raw, "recommendation_source", 60),
        "cost_before_nanos": _money(raw, "estimated_monthly_cost_before_discount"),
        "cost_after_nanos": _money(raw, "estimated_monthly_cost_after_discount"),
        "savings_before_nanos": _money(raw, "estimated_monthly_savings_before_discount"),
        "savings_after_nanos": _money(raw, "estimated_monthly_savings_after_discount"),
        "effort": _choice(raw, "implementation_effort", EFFORTS),
        "restart_needed": _flag(raw, "restart_needed"),
        "rollback_possible": _flag(raw, "rollback_possible"),
        "lookback_days": _lookback(raw, "recommendation_lookback_period_in_days"),
        "last_refresh": _timestamp(raw, "last_refresh_timestamp"),
    }


def parse_co_row(raw: dict) -> dict:
    finding = _choice(raw, "finding", CO_FINDINGS)
    has_option = bool((raw.get("recommendationOptions_1_instanceType") or "").strip())
    if finding == "OVER_PROVISIONED" and not has_option:
        raise _RowError("recommendationOptions_1_instanceType: needed for an OVER_PROVISIONED finding")
    if has_option and (raw.get("recommendationOptions_1_estimatedMonthlySavings_currency") or "").strip() != "USD":
        raise _RowError("recommendationOptions_1_estimatedMonthlySavings_currency: only USD is supported")
    arn = _text(raw, "instanceArn", 200)
    return {
        "instance_arn": arn,
        "resource_id": resource_id_from_arn(arn),
        "account_id": _account(raw, "accountId"),
        "finding": finding,
        "current_type": _text(raw, "currentInstanceType", 60),
        "current_on_demand_nanos": _money(raw, "currentOnDemandPrice"),
        "cpu_max": _percent(raw, "utilizationMetrics_CPU_MAXIMUM"),
        "memory_max": _percent(raw, "utilizationMetrics_MEMORY_MAXIMUM"),
        "recommended_type": _text(raw, "recommendationOptions_1_instanceType", 60, required=False),
        "performance_risk": _percent(raw, "recommendationOptions_1_performanceRisk"),
        "savings_nanos": _money(raw, "recommendationOptions_1_estimatedMonthlySavings_value") if has_option else 0,
        "migration_effort": _text(raw, "recommendationOptions_1_migrationEffort", 20, required=False),
        "lookback_days": _lookback(raw, "lookbackPeriodInDays"),
        "last_refresh": _timestamp(raw, "lastRefreshTimestamp"),
    }


def _validate_manifest(m: dict) -> dict:
    if not isinstance(m, dict):
        raise _Reject("rejected_invalid", "manifest is not a JSON object")
    if m.get("schemaVersion") != OPT_SCHEMA_VERSION:
        raise _Reject("rejected_invalid", f"unsupported schemaVersion {m.get('schemaVersion')!r}")
    for field in ("executionId", "exportTimestamp", "dataFiles"):
        if field not in m:
            raise _Reject("rejected_invalid", f"manifest missing {field}")
    if not _SAFE_ID_RE.match(str(m["executionId"])):
        raise _Reject("rejected_invalid", "executionId has unexpected characters")
    if not _TS_RE.match(str(m["exportTimestamp"])):
        raise _Reject("rejected_invalid", "exportTimestamp must be YYYY-MM-DDTHH:MM:SSZ")
    files = m["dataFiles"]
    if not isinstance(files, list) or not files:
        raise _Reject("rejected_invalid", "dataFiles must be a non-empty list")
    seen: set[str] = set()
    for f in files:
        if not isinstance(f, dict) or not {"key", "sha256", "rowCount", "table"} <= f.keys():
            raise _Reject("rejected_invalid", "each dataFiles entry needs key, sha256, rowCount, table")
        if f["table"] not in TABLES or f["table"] in seen:
            raise _Reject("rejected_invalid", f"unexpected or repeated table {f['table']!r}")
        seen.add(f["table"])
    return m


def _unique(rows: list[dict], key: str) -> None:
    seen: set[str] = set()
    for r in rows:
        if r[key] in seen:
            raise _Reject("rejected_invalid", f"duplicate {key} {r[key]} inside snapshot")
        seen.add(r[key])


def _read_snapshot(source: LocalDirectorySource, ref: DeliveryRef) -> Snapshot:
    try:
        manifest = _validate_manifest(source.read_manifest(ref))
    except FileAccessDenied as exc:
        raise _Reject("rejected_access", str(exc)) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _Reject("rejected_invalid", "manifest is not valid JSON") from exc

    hasher = hashlib.sha256(manifest["executionId"].encode())
    parsed: dict[str, list[dict]] = {t: [] for t in TABLES}
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
        if len(raw_rows) > MAX_ROWS_PER_FILE:
            raise _Reject("rejected_invalid", f"{key} exceeds the row limit for local mode")
        hasher.update(key.encode())
        hasher.update(digest.encode())
        parse = parse_coh_row if f["table"] == "coh" else parse_co_row
        for i, raw in enumerate(raw_rows, start=2):
            try:
                parsed[f["table"]].append(parse(raw))
            except _RowError as exc:
                if len(errors) < 5:
                    errors.append(f"{key} line {i}: {exc}")
    if errors:
        raise _Reject("rejected_invalid", "; ".join(errors))
    _unique(parsed["coh"], "recommendation_id")
    _unique(parsed["compute_optimizer_ec2"], "instance_arn")
    return Snapshot(manifest, parsed["coh"], parsed["compute_optimizer_ec2"], hasher.hexdigest())


def _record(conn, ref: DeliveryRef, manifest: dict | None, status: str, detail: str, *,
            fingerprint: str = "", coh: int = 0, co: int = 0) -> int:
    m = manifest or {}
    cur = conn.execute(
        """INSERT INTO opt_deliveries(source_uri, execution_id, export_timestamp, fingerprint, status, detail,
               coh_rows, co_rows, ingested_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (ref.uri, str(m.get("executionId", "")), str(m.get("exportTimestamp", "")), fingerprint, status, detail,
         coh, co, utc_now()))
    return cur.lastrowid


def _insert(conn, table: str, delivery_id: int, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    conn.executemany(
        f"INSERT INTO {table}(delivery_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
        [(delivery_id, *(r[c] for c in cols)) for r in rows])


def ingest_snapshot(conn, source: LocalDirectorySource, ref: DeliveryRef) -> Outcome:
    try:
        snap = _read_snapshot(source, ref)
    except _Reject as rej:
        try:
            manifest = source.read_manifest(ref)
        except Exception:  # noqa: BLE001 - manifest itself may be the problem
            manifest = None
        manifest = manifest if isinstance(manifest, dict) else None
        with transaction(conn):
            _record(conn, ref, manifest, rej.status, rej.detail)
            _audit(conn, "ingest.optimization." + rej.status, {"uri": ref.uri, "detail": rej.detail})
        return Outcome(rej.status, "", str((manifest or {}).get("executionId", "")), rej.detail, ref.uri)

    exec_id, ts = snap.manifest["executionId"], snap.manifest["exportTimestamp"]
    rows = len(snap.coh) + len(snap.co)
    with transaction(conn):
        prior = conn.execute(
            "SELECT fingerprint FROM opt_deliveries WHERE execution_id=? AND status IN ('active','superseded','stale')",
            (exec_id,)).fetchone()
        if prior:
            if prior["fingerprint"] == snap.fingerprint:
                status, detail = "duplicate", "identical snapshot already ingested; skipped"
            else:
                status, detail = "rejected_conflict", "execution id seen before with different content; not loaded"
            _record(conn, ref, snap.manifest, status, detail, fingerprint=snap.fingerprint)
            _audit(conn, "ingest.optimization." + status, {"uri": ref.uri, "execution_id": exec_id})
            return Outcome(status, "", exec_id, detail, ref.uri, rows)
        active = conn.execute(
            "SELECT id, export_timestamp, execution_id FROM opt_deliveries WHERE status='active'").fetchone()
        if active and active["export_timestamp"] >= ts:
            detail = f"older than active snapshot {active['execution_id']}; skipped"
            _record(conn, ref, snap.manifest, "stale", detail, fingerprint=snap.fingerprint)
            _audit(conn, "ingest.optimization.stale", {"uri": ref.uri, "execution_id": exec_id})
            return Outcome("stale", "", exec_id, detail, ref.uri)
        if active:
            conn.execute("DELETE FROM coh_recommendations")
            conn.execute("DELETE FROM co_ec2_recommendations")
            conn.execute("UPDATE opt_deliveries SET status='superseded' WHERE id=?", (active["id"],))
            detail = f"replaced snapshot {active['execution_id']}"
        else:
            detail = "first optimization snapshot"
        delivery_id = _record(conn, ref, snap.manifest, "active", detail, fingerprint=snap.fingerprint,
                              coh=len(snap.coh), co=len(snap.co))
        _insert(conn, "coh_recommendations", delivery_id, snap.coh)
        _insert(conn, "co_ec2_recommendations", delivery_id, snap.co)
        _audit(conn, "ingest.optimization.active", {"uri": ref.uri, "execution_id": exec_id, "rows": rows})
    return Outcome("active", "", exec_id, detail, ref.uri, rows)


def ingest_optimization(conn, root: str | Path) -> list[Outcome]:
    """Ingest every snapshot under root. A missing folder simply means no snapshots."""
    if not Path(root).exists():
        return []
    source = LocalDirectorySource(root)
    return [ingest_snapshot(conn, source, ref) for ref in source.list_deliveries()]
