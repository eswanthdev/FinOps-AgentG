"""SQLite store. One database file = one tenant (see docs/SECURITY.md)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_uri TEXT NOT NULL,
    export_name TEXT NOT NULL,
    billing_period TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    export_timestamp TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    unblended_total_nanos INTEGER NOT NULL DEFAULT 0,
    period_complete INTEGER NOT NULL DEFAULT 0,
    data_through TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliveries_period ON deliveries(export_name, billing_period, status);

CREATE TABLE IF NOT EXISTS line_items (
    delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
    billing_period TEXT NOT NULL,
    line_item_id TEXT NOT NULL,
    payer_account_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    line_item_type TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    service TEXT NOT NULL,
    usage_type TEXT NOT NULL,
    operation TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    region TEXT NOT NULL,
    usage_amount TEXT NOT NULL,
    unblended_nanos INTEGER NOT NULL,
    on_demand_nanos INTEGER NOT NULL,
    amortized_nanos INTEGER,
    resource_tags TEXT NOT NULL,
    PRIMARY KEY (billing_period, line_item_id)
);
CREATE INDEX IF NOT EXISTS ix_li_date ON line_items(usage_date);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    account_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anomaly_states (
    anomaly_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    source_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id INTEGER NOT NULL,
    threshold_key TEXT NOT NULL,
    period_start TEXT NOT NULL,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    delivery_detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (budget_id, threshold_key, period_start)
);

CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    query_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opt_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_uri TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    export_timestamp TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    coh_rows INTEGER NOT NULL DEFAULT 0,
    co_rows INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coh_recommendations (
    delivery_id INTEGER NOT NULL REFERENCES opt_deliveries(id),
    recommendation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    region TEXT NOT NULL,
    resource_arn TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    current_resource_type TEXT NOT NULL,
    current_summary TEXT NOT NULL,
    recommended_resource_type TEXT NOT NULL,
    recommended_summary TEXT NOT NULL,
    recommended_details TEXT NOT NULL,
    source TEXT NOT NULL,
    cost_before_nanos INTEGER NOT NULL,
    cost_after_nanos INTEGER NOT NULL,
    savings_before_nanos INTEGER NOT NULL,
    savings_after_nanos INTEGER NOT NULL,
    effort TEXT NOT NULL,
    restart_needed INTEGER NOT NULL,
    rollback_possible INTEGER NOT NULL,
    lookback_days INTEGER NOT NULL,
    last_refresh TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS co_ec2_recommendations (
    delivery_id INTEGER NOT NULL REFERENCES opt_deliveries(id),
    instance_arn TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    finding TEXT NOT NULL,
    current_type TEXT NOT NULL,
    current_on_demand_nanos INTEGER NOT NULL,
    cpu_max TEXT NOT NULL,
    memory_max TEXT NOT NULL,
    recommended_type TEXT NOT NULL,
    performance_risk TEXT NOT NULL,
    savings_nanos INTEGER NOT NULL,
    migration_effort TEXT NOT NULL,
    lookback_days INTEGER NOT NULL,
    last_refresh TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opt_states (
    finding_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def purge_audit(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete audit events older than the retention window. Returns rows removed."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).replace(microsecond=0).isoformat()
    cur = conn.execute("DELETE FROM audit_events WHERE ts < ?", (cutoff,))
    return cur.rowcount
