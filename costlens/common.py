"""Common resource/opportunity database with immutable source evidence.

Deduplication changes presentation, never deletes source records or adds source
savings. Source claims remain unvalidated; existing Optimize owns validated totals.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .money import parse_usd_to_nanos
from .query.athena import display
from .store import transaction

SOURCES = ("cost-optimization-hub", "compute-optimizer", "trusted-advisor", "savings-plans")
SCHEMA = """
CREATE TABLE IF NOT EXISTS common_generations (
 workspace_id TEXT NOT NULL, generation_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 fingerprint TEXT NOT NULL, status TEXT NOT NULL,
 PRIMARY KEY(workspace_id, generation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS common_active_generation ON common_generations(workspace_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS common_evidence (
 workspace_id TEXT NOT NULL, evidence_id TEXT NOT NULL, source TEXT NOT NULL, native_id TEXT NOT NULL,
 account_id TEXT NOT NULL, resource_key TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(workspace_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS common_generation_evidence (
 workspace_id TEXT NOT NULL, generation_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 PRIMARY KEY(workspace_id,generation_id,evidence_id),
 FOREIGN KEY(workspace_id,generation_id) REFERENCES common_generations(workspace_id,generation_id),
 FOREIGN KEY(workspace_id,evidence_id) REFERENCES common_evidence(workspace_id,evidence_id)
);
CREATE TABLE IF NOT EXISTS common_resources (
 workspace_id TEXT NOT NULL, resource_key TEXT NOT NULL, account_id TEXT NOT NULL, resource_arn TEXT NOT NULL,
 PRIMARY KEY(workspace_id,resource_key)
);
CREATE TABLE IF NOT EXISTS common_opportunities (
 workspace_id TEXT NOT NULL, opportunity_id TEXT NOT NULL, account_id TEXT NOT NULL, resource_key TEXT NOT NULL,
 resource_arn TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, state TEXT NOT NULL,
 selected_source TEXT NOT NULL, selected_claim_nanos INTEGER, source_claim_display TEXT NOT NULL,
 claim_sources_json TEXT NOT NULL, evidence_json TEXT NOT NULL, conflict_group TEXT NOT NULL,
 has_alternatives INTEGER NOT NULL, validation_state TEXT NOT NULL, counted INTEGER NOT NULL DEFAULT 0,
 review_status TEXT NOT NULL DEFAULT 'unreviewed', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 PRIMARY KEY(workspace_id,opportunity_id)
);
"""


def _hash(*values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def _resource(workspace, account, arn):
    if not arn:
        return ""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or parts[4] != account or not parts[5]:
        raise ValueError("resource ARN does not match its source account")
    return _hash(workspace, arn)


def _records(workspace, sources, allowed_accounts):
    records = []
    for source in SOURCES:
        seen = set()
        for raw in sources[source]:
            if not isinstance(raw, dict):
                raise ValueError("source rows must be objects")
            payload = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            account = raw.get("account_id", raw.get("accountId", ""))
            if account not in allowed_accounts:
                raise PermissionError("source record is outside the configured accounts")
            arn = raw.get("resource_arn", raw.get("instanceArn", ""))
            native = raw.get("recommendation_id", raw.get("instanceArn", ""))
            if not native or (account, native) in seen:
                raise ValueError("source native IDs must be present and unique within a snapshot")
            seen.add((account, native))
            action, target, claim, basis = "", "", None, ""
            if source == "cost-optimization-hub":
                action = raw.get("action_type", "")
                target = raw.get("recommended_resource_summary", "")
                if raw.get("currency_code") != "USD":
                    raise ValueError("only USD claims are supported")
                claim = parse_usd_to_nanos(raw["estimated_monthly_savings_after_discount"])
                basis = "after_discount"
            elif source == "compute-optimizer" and raw.get("finding") == "OVER_PROVISIONED" and raw.get("recommendationOptions_1_instanceType"):
                action, target = "Rightsize", raw["recommendationOptions_1_instanceType"]
                if raw.get("recommendationOptions_1_estimatedMonthlySavings_currency") != "USD":
                    raise ValueError("only USD claims are supported")
                claim = parse_usd_to_nanos(raw["recommendationOptions_1_estimatedMonthlySavings_value"])
                basis = "on_demand"
            elif source == "savings-plans":
                action = "PurchaseSavingsPlans"
                target = "|".join(str(raw.get(k, "")) for k in ("scope", "term", "payment_option"))
                if raw.get("currency_code") != "USD":
                    raise ValueError("only USD claims are supported")
                claim = parse_usd_to_nanos(raw["estimated_monthly_savings"])
                basis = "after_discount"
            if claim is not None and claim < 0:
                raise ValueError("negative source savings claim")
            key = _resource(workspace, account, arn)
            records.append(dict(source=source, native_id=native, account_id=account, resource_arn=arn,
                resource_key=key, action=action, target=target, claim=claim, basis=basis, raw=raw,
                evidence_id=_hash(workspace, source, native, payload), payload=payload))
    return records


def synchronize(conn, *, workspace_id: str, sources: dict, allowed_accounts, observed_at: str, complete: bool):
    if set(sources) != set(SOURCES) or complete is not True:
        raise ValueError("all four complete recommendation source snapshots are required")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", workspace_id):
        raise ValueError("invalid trusted workspace identity")
    stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("source observation timestamp needs a timezone")
    observed_at = stamp.astimezone(timezone.utc).isoformat()
    records = _records(workspace_id, sources, set(allowed_accounts))
    fingerprint = _hash(sorted(r["evidence_id"] for r in records))
    generation = _hash(workspace_id, observed_at, fingerprint)
    by_coh = {(r["account_id"], r["native_id"]): r for r in records if r["source"] == "cost-optimization-hub"}
    for record in records:
        link = record["raw"].get("linked_coh_recommendation_id")
        if link:
            original = by_coh.get((record["account_id"], link))
            if original is None or (record["resource_key"] and record["resource_key"] != original["resource_key"]):
                raise ValueError("explicit source link does not resolve to the same account/resource")
            if record["action"] and record["action"] != original["action"]:
                raise ValueError("explicit source link conflicts with recommendation action")
            if record["source"] == "savings-plans":
                details = json.loads(original["raw"].get("recommended_resource_details", "{}"))
                for raw_key, detail_key in (("term","term"),("payment_option","paymentOption")):
                    if details.get(detail_key) and record["raw"].get(raw_key) != details[detail_key]:
                        raise ValueError("explicit commitment link has different purchase terms")
                if details.get("hourlyCommitment") and Decimal(str(record["raw"].get("hourly_commitment"))) != Decimal(str(details["hourlyCommitment"])):
                    raise ValueError("explicit commitment link has different hourly commitment")
            record.update(resource_key=original["resource_key"], resource_arn=original["resource_arn"],
                          action=original["action"], target=original["target"], link=original["evidence_id"])
        if not record["resource_key"] and record["action"]:
            # An account-level purchase is not merged with other purchases merely
            # because its resource ARN is empty. Only a validated explicit link joins it.
            record["resource_key"] = _hash(workspace_id, record["account_id"], record.get("link", record["native_id"]))
    for record in records:
        if record.get("link") and not record["resource_arn"]:
            original = by_coh[(record["account_id"], record["raw"]["linked_coh_recommendation_id"])]
            record["resource_key"] = original["resource_key"]
    groups = {}
    supporting = {}
    for record in records:
        if record["resource_key"]:
            supporting.setdefault(record["resource_key"], []).append(record)
        if record["action"]:
            key = (record["resource_key"], record["action"], record["target"])
            groups.setdefault(key, []).append(record)
    priority = {source: index for index, source in enumerate(SOURCES)}
    normalized = []
    for (resource, action, target), members in groups.items():
        chosen = min(members, key=lambda r: (r["claim"] is None, priority[r["source"]], r["native_id"]))
        same_basis_claims = {r["claim"] for r in members if r["claim"] is not None and r["basis"] == chosen["basis"]}
        evidence = [{"evidence_id": r["evidence_id"], "source": r["source"], "native_id": r["native_id"],
                     "relation": "claim" if r in members else "resource_context"} for r in supporting[resource]]
        normalized.append(dict(workspace_id=workspace_id, opportunity_id=_hash(workspace_id, resource, action, target),
            account_id=chosen["account_id"], resource_key=resource, resource_arn=chosen["resource_arn"], action=action,
            target=target, selected_source=chosen["source"], selected_claim_nanos=chosen["claim"],
            source_claim_display=display(chosen["claim"]) if chosen["claim"] is not None else "Not estimated",
            claim_sources_json=json.dumps(sorted({r["source"] for r in members})), evidence_json=json.dumps(evidence,sort_keys=True),
            conflict_group=resource, has_alternatives=int(sum(1 for key in groups if key[0] == resource) > 1),
            validation_state="conflicting_source_claims" if len(same_basis_claims) > 1 else "needs_validation", counted=0))
    conn.executescript(SCHEMA)
    with transaction(conn):
        active = conn.execute("SELECT * FROM common_generations WHERE workspace_id=? AND status='active'", (workspace_id,)).fetchone()
        if active and active["generation_id"] == generation:
            return {"status": "duplicate", "source_records": len(records), "opportunities": len(normalized)}
        if active and active["observed_at"] > observed_at:
            return {"status": "stale", "source_records": len(records), "opportunities": len(normalized)}
        if active and active["observed_at"] == observed_at and active["fingerprint"] != fingerprint:
            raise ValueError("same snapshot timestamp has conflicting contents")
        conn.execute("UPDATE common_generations SET status='superseded' WHERE workspace_id=? AND status='active'", (workspace_id,))
        conn.execute("INSERT INTO common_generations VALUES (?,?,?,?, 'active')", (workspace_id,generation,observed_at,fingerprint))
        for record in records:
            conn.execute("INSERT OR IGNORE INTO common_evidence VALUES (?,?,?,?,?,?,?)", (workspace_id, record["evidence_id"],
                record["source"],record["native_id"],record["account_id"],record["resource_key"],record["payload"]))
            conn.execute("INSERT INTO common_generation_evidence VALUES (?,?,?)", (workspace_id,generation,record["evidence_id"]))
            if record["resource_arn"]:
                conn.execute("INSERT OR IGNORE INTO common_resources VALUES (?,?,?,?)", (workspace_id,record["resource_key"],record["account_id"],record["resource_arn"]))
        conn.execute("UPDATE common_opportunities SET state='retired' WHERE workspace_id=?", (workspace_id,))
        for row in normalized:
            columns = list(row)
            updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in ("workspace_id","opportunity_id"))
            conn.execute(f"INSERT INTO common_opportunities ({','.join(columns)},state,first_seen,last_seen) VALUES ({','.join('?' for _ in columns)},'active',?,?) "
                         f"ON CONFLICT(workspace_id,opportunity_id) DO UPDATE SET {updates},state='active',last_seen=excluded.last_seen",
                         [*[row[key] for key in columns],observed_at,observed_at])
    return {"status":"active", "generation_id":generation,"source_records":len(records),"opportunities":len(normalized),
            "note":"One canonical opportunity per resource/action/target; source claims are not counted savings."}


def export_parquet(conn, workspace_id: str, path: str | Path, *, synthetic: bool):
    from .parquet_fixtures import arrow_modules
    pa,pq = arrow_modules()
    rows = [dict(row) for row in conn.execute("SELECT * FROM common_opportunities WHERE workspace_id=? AND state='active' ORDER BY opportunity_id",(workspace_id,))]
    if not rows:
        raise ValueError("no canonical opportunities to publish")
    for row in rows:
        row["synthetic_data"] = synthetic
        row["counted"] = bool(row["counted"])
        row["has_alternatives"] = bool(row["has_alternatives"])
    table = pa.Table.from_pylist(rows).replace_schema_metadata({b"synthetic_data":str(synthetic).lower().encode(),b"contract":b"costlens-common-opportunities-v1"})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("never overwrite an existing canonical publication")
    pq.write_table(table,path,compression="snappy")
    return {"rows":len(rows),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
