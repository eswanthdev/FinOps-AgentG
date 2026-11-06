import copy
import json

import pytest

from costlens.common import synchronize
from costlens import fixtures
from costlens.parquet_fixtures import _extra_sources


def bundle():
    extra = _extra_sources()
    return {"cost-optimization-hub":fixtures.coh_rows(),"compute-optimizer":fixtures.co_rows(),
            "trusted-advisor":extra["trusted-advisor"],"savings-plans":extra["savings-plans"]}


def sync(conn, data=None, workspace="pilot", timestamp="2026-11-03T00:00:00Z", complete=True):
    return synchronize(conn,workspace_id=workspace,sources=data if data is not None else bundle(),
                       allowed_accounts=fixtures.ACCOUNTS,observed_at=timestamp,complete=complete)


def test_common_database_deduplicates_and_retains_every_source(fresh_conn):
    data = bundle()
    result = sync(fresh_conn,data)
    assert result["source_records"] == sum(len(rows) for rows in data.values())
    assert result["opportunities"] < result["source_records"]
    assert fresh_conn.execute("SELECT COUNT(*) FROM common_evidence").fetchone()[0] == result["source_records"]
    row = fresh_conn.execute("SELECT * FROM common_opportunities WHERE resource_arn LIKE '%i-0c0a0000m6i' AND action='Rightsize'").fetchone()
    assert set(json.loads(row["claim_sources_json"])) == {"cost-optimization-hub","compute-optimizer"}
    assert row["counted"] == 0 and row["selected_source"] == "cost-optimization-hub"
    assert len(json.loads(row["evidence_json"])) >= 2


def test_alternatives_and_commitments_do_not_merge_with_resource_changes(fresh_conn):
    sync(fresh_conn)
    rows = fresh_conn.execute("SELECT * FROM common_opportunities WHERE resource_arn LIKE '%i-0d0e0000c6i'").fetchall()
    assert {r["action"] for r in rows} == {"Rightsize","MigrateToGraviton"}
    assert all(r["has_alternatives"] for r in rows)
    assert len({r["conflict_group"] for r in rows}) == 1
    plans = fresh_conn.execute("SELECT * FROM common_opportunities WHERE action='PurchaseSavingsPlans'").fetchall()
    assert len(plans) == 1
    assert set(json.loads(plans[0]["claim_sources_json"])) == {"cost-optimization-hub","savings-plans"}


def test_repeated_snapshot_and_review_state_survive_refresh(fresh_conn):
    sync(fresh_conn)
    fresh_conn.execute("UPDATE common_opportunities SET review_status='assigned'")
    assert sync(fresh_conn)["status"] == "duplicate"
    sync(fresh_conn,timestamp="2026-11-04T00:00:00Z")
    assert {r[0] for r in fresh_conn.execute("SELECT review_status FROM common_opportunities")} == {"assigned"}
    assert fresh_conn.execute("SELECT COUNT(*) FROM common_generations").fetchone()[0] == 2


def test_bad_explicit_link_and_partial_snapshot_keep_active_generation(fresh_conn):
    sync(fresh_conn)
    data = bundle()
    data["trusted-advisor"][0]["linked_coh_recommendation_id"] = "missing"
    with pytest.raises(ValueError):
        sync(fresh_conn,data,timestamp="2026-11-04T00:00:00Z")
    with pytest.raises(ValueError):
        sync(fresh_conn,complete=False)
    assert fresh_conn.execute("SELECT COUNT(*) FROM common_generations").fetchone()[0] == 1


def test_workspace_and_account_boundaries(fresh_conn):
    sync(fresh_conn)
    sync(fresh_conn,workspace="other")
    sets = [{r[0] for r in fresh_conn.execute("SELECT opportunity_id FROM common_opportunities WHERE workspace_id=?",(workspace,))}
            for workspace in ("pilot","other")]
    assert not sets[0] & sets[1]
    data = bundle()
    data["cost-optimization-hub"][0]["account_id"] = "999999999999"
    with pytest.raises(PermissionError):
        sync(fresh_conn,data)


def test_conflicting_claims_not_added(fresh_conn):
    data = bundle()
    duplicate = dict(data["cost-optimization-hub"][0])
    duplicate.update(recommendation_id="another-source-id",estimated_monthly_savings_after_discount="99.00")
    data["cost-optimization-hub"].append(duplicate)
    sync(fresh_conn,data)
    row = fresh_conn.execute("SELECT * FROM common_opportunities WHERE validation_state='conflicting_source_claims'").fetchone()
    assert row is not None and row["counted"] == 0


def test_retired_opportunities_keep_evidence(fresh_conn):
    first = sync(fresh_conn)
    data = {source:[] for source in bundle()}
    sync(fresh_conn,data,timestamp="2026-11-04T00:00:00Z")
    assert fresh_conn.execute("SELECT COUNT(*) FROM common_opportunities WHERE state='active'").fetchone()[0] == 0
    assert fresh_conn.execute("SELECT COUNT(*) FROM common_evidence").fetchone()[0] == first["source_records"]


def test_commitment_link_cannot_hide_different_terms(fresh_conn):
    data = bundle()
    data["savings-plans"][0]["term"] = "THREE_YEARS"
    with pytest.raises(ValueError, match="purchase terms"):
        sync(fresh_conn,data)
