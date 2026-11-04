"""Data status: what data we have, how fresh it is, and what is not connected."""
from __future__ import annotations

from datetime import date

from .query import engine
from .store import get_meta

# Sources the product will use. CUR 2.0, Cost Optimization Hub and Compute Optimizer are read from local files.
SOURCES = [
    ("cur2", "CUR 2.0 (Data Exports)", "Cost and usage line items"),
    ("coh", "Cost Optimization Hub", "Savings recommendations (needed by Optimize module)"),
    ("compute_optimizer", "Compute Optimizer", "Rightsizing evidence"),
    ("organizations", "AWS Organizations", "Accounts, OUs and existing SCPs (needed for guardrail drafts)"),
    ("storage_lens", "S3 Storage Lens", "Storage usage evidence"),
    ("trusted_advisor", "Trusted Advisor", "Checks and idle resources"),
    ("cost_explorer", "Cost Explorer API", "Forecasts and RI/SP utilisation"),
]


def data_status(conn, source_label: str | None = None) -> dict:
    ref = engine.reference_date(conn)
    expected_start = get_meta(conn, "expected_start_period") or None
    mode = get_meta(conn, "data_mode", "unknown")
    deliveries = [dict(r) for r in conn.execute(
        "SELECT billing_period, execution_id, export_timestamp, status, detail, row_count, period_complete, "
        "data_through, ingested_at FROM deliveries ORDER BY id")]
    active = [d for d in deliveries if d["status"] == "active"]
    start = date.fromisoformat((expected_start or (min((d["billing_period"] for d in active), default=None)
                                                   or ref.isoformat()[:7])) + "-01")
    cov = engine.coverage(conn, start, ref if ref > start else start.replace(day=2))
    data_through = cov["data_through"]
    lag_days = (ref - date.fromisoformat(data_through)).days if data_through else None
    accounts = engine.account_names(conn)
    opt = conn.execute("SELECT execution_id, coh_rows, co_rows FROM opt_deliveries WHERE status='active'").fetchone()
    sources = []
    for key, name, purpose in SOURCES:
        if key == "cur2":
            state = "connected" if active else "no_data"
            detail = (f"{source_label or 'local folder'} · {len(active)} billing period(s) loaded"
                      if active else "No deliveries ingested yet")
        elif key in ("coh", "compute_optimizer") and opt:
            rows = opt["coh_rows"] if key == "coh" else opt["co_rows"]
            state = "connected" if rows else "no_data"
            detail = f"{'Synthetic local' if mode == 'synthetic' else 'Local'} snapshot {opt['execution_id']} · {rows} rows"
        elif key == "organizations" and get_meta(conn, "org_structure"):
            state, detail = "connected", f"{'Synthetic local' if mode == 'synthetic' else 'Local'} organization file"
        else:
            state, detail = "not_connected", "Not connected in this release"
        sources.append({"key": key, "name": name, "purpose": purpose, "state": state, "detail": detail})
    issues = [d for d in deliveries if d["status"].startswith("rejected_")]
    return {
        "data_mode": mode,
        "synthetic": mode == "synthetic",
        "reference_date": ref.isoformat(),
        "data_through": data_through,
        "lag_days": lag_days,
        "freshness": ("unknown" if lag_days is None else "fresh" if lag_days <= 2 else "stale"),
        "accounts": [{"id": k, "name": v} for k, v in sorted(accounts.items(), key=lambda x: x[1])],
        "coverage": cov,
        "sources": sources,
        "deliveries": deliveries,
        "delivery_issues": issues,
        "permissions": {"aws_access": "none", "detail": "Local mode: the app makes no AWS calls and holds no AWS credentials."},
    }
