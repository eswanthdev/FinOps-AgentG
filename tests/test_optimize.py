"""Cost Optimization Engineer: ingestion, evidence, overlaps, conflicts and money edge cases."""
import json
from decimal import Decimal

import pytest

from costlens import fixtures, store
from costlens.agent import optimize
from costlens.agent.orchestrator import ACTIVE_WORKERS, Orchestrator, PLANNED_WORKERS
from costlens.agent.optimize import DataQualityWorker, monthly_run_rate, scaled
from costlens.ingest.loader import ingest_source
from costlens.ingest.optimization import ingest_optimization, parse_coh_row, resource_id_from_arn
from costlens.ingest.sources import LocalDirectorySource
from costlens.money import NANOS_PER_USD, parse_usd_to_nanos
from costlens.query import engine
from costlens.query.model import CostQuery

NOW = "2026-11-02T06:00:00Z"


def usd(text: str) -> int:
    return parse_usd_to_nanos(text)


def by_rec(summary: dict) -> dict:
    return {f["recommendation_id"]: f for f in summary["opportunities"]}


@pytest.fixture()
def cur_only(fresh_conn, fixture_root):
    """CUR loaded, no optimization snapshot."""
    ingest_source(fresh_conn, LocalDirectorySource(fixture_root))
    store.set_meta(fresh_conn, "reference_date", fixtures.REFERENCE_DATE.isoformat())
    return fresh_conn


def snapshot(conn, tmp_path, coh, co, name="001-s", execution="opt-s", exported=NOW):
    fixtures.write_optimization_snapshot(tmp_path / "opt", name, execution, exported, coh, co)
    return ingest_optimization(conn, tmp_path / "opt")[-1]


def only(rec_ids: set[str]) -> list[dict]:
    return [r for r in fixtures.coh_rows() if r["recommendation_id"] in rec_ids]


# ------------------------------------------------------------------ ingestion

def test_demo_snapshot_is_ingested(demo_conn):
    row = demo_conn.execute("SELECT * FROM opt_deliveries WHERE status='active'").fetchone()
    assert row["coh_rows"] == len(fixtures.coh_rows()) and row["co_rows"] == len(fixtures.co_rows())


def test_resource_id_from_arn():
    assert resource_id_from_arn("arn:aws:ec2:us-east-1:210987654321:instance/i-1") == "i-1"
    assert resource_id_from_arn("arn:aws:rds:us-east-1:210987654321:db:db-x") == "db-x"
    assert resource_id_from_arn("") == ""


def test_redelivery_is_duplicate_and_newer_snapshot_replaces(cur_only, tmp_path):
    coh, co = fixtures.coh_rows(), fixtures.co_rows()
    assert snapshot(cur_only, tmp_path, coh, co).status == "active"
    fixtures.write_optimization_snapshot(tmp_path / "again", "001-s", "opt-s", NOW, coh, co)
    assert ingest_optimization(cur_only, tmp_path / "again")[-1].status == "duplicate"
    assert cur_only.execute("SELECT COUNT(*) FROM coh_recommendations").fetchone()[0] == len(coh)
    fixtures.write_optimization_snapshot(tmp_path / "new", "002-s", "opt-new", "2026-11-03T06:00:00Z", coh[:3], co)
    assert ingest_optimization(cur_only, tmp_path / "new")[-1].status == "active"
    assert cur_only.execute("SELECT COUNT(*) FROM coh_recommendations").fetchone()[0] == 3
    fixtures.write_optimization_snapshot(tmp_path / "old", "003-s", "opt-old", "2026-10-01T06:00:00Z", coh, co)
    assert ingest_optimization(cur_only, tmp_path / "old")[-1].status == "stale"


def test_same_execution_with_different_content_is_rejected(cur_only, tmp_path):
    snapshot(cur_only, tmp_path, fixtures.coh_rows(), fixtures.co_rows())
    fixtures.write_optimization_snapshot(tmp_path / "x", "001-s", "opt-s", NOW, fixtures.coh_rows()[:2], None)
    assert ingest_optimization(cur_only, tmp_path / "x")[-1].status == "rejected_conflict"


def test_truncated_file_is_rejected_and_previous_snapshot_is_kept(cur_only, tmp_path):
    snapshot(cur_only, tmp_path, fixtures.coh_rows(), fixtures.co_rows())
    out = fixtures.write_optimization_snapshot(tmp_path / "bad", "002-s", "opt-bad", "2026-11-03T06:00:00Z",
                                               fixtures.coh_rows(), None)
    (out / "coh-recommendations.csv").write_bytes(b"recommendation_id\n")
    assert ingest_optimization(cur_only, tmp_path / "bad")[-1].status == "rejected_incomplete"
    assert cur_only.execute("SELECT COUNT(*) FROM coh_recommendations").fetchone()[0] == len(fixtures.coh_rows())
    assert any("rejected" in n for n in optimize.summary(cur_only)["snapshot"]["notes"])


@pytest.mark.parametrize("column,value", [
    ("estimated_monthly_savings_after_discount", "-1.00"),
    ("estimated_monthly_savings_after_discount", "NaN"),
    ("estimated_monthly_savings_after_discount", ""),
    ("estimated_monthly_savings_after_discount", "20000000000"),
    ("currency_code", "EUR"),
    ("account_id", "123"),
    ("action_type", "DeleteEverything"),
    ("implementation_effort", "Easy"),
    ("recommendation_lookback_period_in_days", "0"),
    ("restart_needed", "maybe"),
    ("recommended_resource_details", "[1]"),
])
def test_bad_rows_reject_the_whole_snapshot(cur_only, tmp_path, column, value):
    coh = fixtures.coh_rows()
    coh[0][column] = value
    assert snapshot(cur_only, tmp_path, coh, None).status == "rejected_invalid"
    assert cur_only.execute("SELECT COUNT(*) FROM coh_recommendations").fetchone()[0] == 0


def test_duplicate_recommendation_id_is_rejected(cur_only, tmp_path):
    coh = fixtures.coh_rows()
    assert snapshot(cur_only, tmp_path, coh + coh[:1], None).status == "rejected_invalid"


# ------------------------------------------------------------------ totals and overlaps

def test_counted_total_matches_hand_calculation(demo_conn):
    t = optimize.summary(demo_conn)["totals"]
    expected = (usd("734.38")          # g5 stop
                + 8 * usd("248.20")    # ETL rightsizing
                + 2 * usd("183.96")    # r6i, Hub and Compute Optimizer-only
                + 2 * usd("70.08")     # on-demand m6i
                + usd("44.00"))        # gp3
    assert t["counted_nanos"] == expected
    assert t["counted_display"] == "$3,272.06"
    assert t["counted_count"] == 14


def test_each_resource_is_counted_once(demo_conn):
    counted = [f for f in optimize.summary(demo_conn)["opportunities"] if f["counted"]]
    keys = [(f["account_id"], f["resource_id"]) for f in counted]
    assert len(keys) == len(set(keys))


def test_competing_recommendations_keep_only_the_best(demo_conn):
    f = by_rec(optimize.summary(demo_conn))
    assert f["coh-etl-0"]["counted"] and f["coh-etl-0"]["phase"] == "plan_ready"
    alt = f["coh-etl-grav-0"]
    assert alt["phase"] == "alternative" and not alt["counted"] and alt["savings_nanos"] is None
    assert alt["overlaps"] == [f["coh-etl-0"]["id"]]


def test_alternative_money_is_not_in_the_total(demo_conn):
    t = optimize.summary(demo_conn)["totals"]
    assert t["alternative_nanos"] == 2 * usd("73.00")
    assert t["raw_nanos"] > t["counted_nanos"] + t["alternative_nanos"]


def test_hub_and_compute_optimizer_same_advice_is_one_finding(demo_conn):
    ids = [f["recommendation_id"] for f in optimize.summary(demo_conn)["opportunities"]
           if f["resource_id"] == "i-0d0a0000r6i"]
    assert ids == ["coh-r6i-0"]


def test_compute_optimizer_only_finding_is_used_with_lower_confidence(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["co-i-0d0a0001r6i"]
    assert f["counted"] and f["confidence"] == "low"
    assert any("High performance risk" in r for r in f["risks"])


def test_optimized_instances_produce_no_finding(demo_conn):
    assert "co-i-0d0a0002r6i" not in by_rec(optimize.summary(demo_conn))


def test_dismissing_the_winner_promotes_the_alternative_without_double_counting(demo_conn):
    before = by_rec(optimize.summary(demo_conn))
    optimize.set_status(demo_conn, before["coh-etl-0"]["id"], "dismissed")
    s = optimize.summary(demo_conn)
    f = by_rec(s)
    assert not f["coh-etl-0"]["counted"]
    assert f["coh-etl-grav-0"]["counted"] and f["coh-etl-grav-0"]["phase"] == "plan_ready"
    assert s["totals"]["counted_nanos"] == usd("3272.06") - usd("248.20") + usd("73.00")


# ------------------------------------------------------------------ rightsizing vs Savings Plan

def test_rightsizing_a_covered_instance_is_conditional_and_not_counted(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["coh-m6i-0"]
    assert f["sp_covered"] and f["phase"] == "conditional" and not f["counted"]
    assert f["savings_nanos"] is None
    assert f["monthly_savings_display"] == "Up to $49.06"
    assert f["confidence"] == "medium"
    assert any("commitment" in r for r in f["risks"])
    assert "absorb" in f["action_plan"]["prerequisites"][0]


def test_covered_instance_uses_after_discount_not_on_demand_savings(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["coh-m6i-0"]
    assert f["claim_nanos"] == usd("49.056") and f["claim_nanos"] < usd("70.08")


def test_on_demand_twin_of_a_covered_instance_is_counted(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["coh-m6i-4"]
    assert not f["sp_covered"] and f["counted"]


def test_savings_plan_proposal_is_scaled_after_rightsizing(demo_conn):
    s = optimize.summary(demo_conn)
    f = by_rec(s)["coh-sp-data"]
    removed = 8 * usd("248.20") + 2 * usd("183.96")
    assert f["phase"] == "proposal" and not f["counted"]
    assert f["removed_nanos"] == removed
    assert f["adjusted_nanos"] == usd("1419.12") * (usd("5069.12") - removed) // usd("5069.12")
    assert f["adjusted_nanos"] < f["claim_nanos"]
    assert f["adjusted_hourly_display"] == "$2.68"


def test_proposal_is_never_added_to_the_counted_total(demo_conn):
    t = optimize.summary(demo_conn)["totals"]
    assert t["proposal_nanos"] > 0
    assert t["counted_nanos"] == usd("3272.06")


def test_commitment_is_proposal_only_and_flags_unused_commitment(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["coh-sp-data"]
    assert f["action_plan"]["kind"] == "commitment_proposal"
    assert "never buys" in f["action_plan"]["execution"]
    assert f["confidence"] == "low"
    assert any("unused commitment" in r for r in f["confidence_reasons"])


def test_proposal_grows_back_if_rightsizing_is_dismissed(demo_conn):
    before = by_rec(optimize.summary(demo_conn))
    optimize.set_status(demo_conn, before["coh-r6i-0"]["id"], "dismissed")
    after = by_rec(optimize.summary(demo_conn))
    assert after["coh-sp-data"]["adjusted_nanos"] > before["coh-sp-data"]["adjusted_nanos"]


# ------------------------------------------------------------------ missing evidence

@pytest.mark.parametrize("rec_id,phrase", [
    ("coh-gone", "no cost in the CUR"),
    ("coh-rds-prod", "No utilisation evidence"),
    ("coh-t3-conflict", "Sources disagree"),
    ("coh-t3-overclaim", "more than the"),
    ("coh-t3-short", "only 3 days"),
])
def test_weak_evidence_gets_no_savings_number(demo_conn, rec_id, phrase):
    f = by_rec(optimize.summary(demo_conn))[rec_id]
    assert f["phase"] == "needs_evidence" and not f["counted"]
    assert f["savings_nanos"] is None and f["monthly_savings_display"] == "Not estimated"
    assert f["confidence"] == "insufficient" and f["action_plan"] is None
    assert any(phrase in b for b in f["blockers"])


def test_without_compute_optimizer_rightsizing_has_no_estimate(cur_only, tmp_path):
    snapshot(cur_only, tmp_path, only({"coh-m6i-4", "coh-ebs-gp3"}), None)
    f = by_rec(optimize.summary(cur_only))
    assert f["coh-m6i-4"]["phase"] == "needs_evidence"
    assert f["coh-ebs-gp3"]["counted"]  # a price change does not need utilisation evidence


def test_no_snapshot_means_no_findings_and_no_number(cur_only):
    s = optimize.summary(cur_only)
    assert s["state"] == "no_data" and s["opportunities"] == [] and s["totals"]["counted_nanos"] == 0
    r = Orchestrator(cur_only).ask("How can I reduce EC2 cost?")
    assert r["kind"] == "optimize" and "$" not in r["message"]


def test_snapshot_without_cur_cannot_be_verified(fresh_conn, tmp_path):
    snapshot(fresh_conn, tmp_path, fixtures.coh_rows(), fixtures.co_rows())
    s = optimize.summary(fresh_conn)
    assert s["state"] == "no_data" and "CUR" in s["reason"]


def test_missing_memory_metric_lowers_confidence(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["coh-etl-1"]
    assert f["confidence"] == "medium"
    assert any("Memory" in r for r in f["confidence_reasons"])


def test_short_cost_history_lowers_confidence(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["coh-etl-1"]
    assert f["days_with_usage"] == 19
    assert any("19 days" in r for r in f["confidence_reasons"])


def test_stale_snapshot_lowers_confidence(cur_only, tmp_path):
    snapshot(cur_only, tmp_path, fixtures.coh_rows(), fixtures.co_rows(), exported="2026-09-01T06:00:00Z")
    s = optimize.summary(cur_only)
    assert s["snapshot"]["state"] == "stale"
    assert by_rec(s)["coh-g5-stop"]["confidence"] == "low"


def test_stop_with_busy_cpu_is_blocked(cur_only, tmp_path):
    co = fixtures.co_rows()
    for r in co:
        if "i-0f0a0001g5" in r["instanceArn"]:
            r["utilizationMetrics_CPU_MAXIMUM"] = "55.0"
    snapshot(cur_only, tmp_path, only({"coh-g5-stop"}), co)
    assert by_rec(optimize.summary(cur_only))["coh-g5-stop"]["phase"] == "needs_evidence"


def test_commitment_with_wrong_baseline_needs_evidence(cur_only, tmp_path):
    coh = only({"coh-sp-data"})
    coh[0]["estimated_monthly_cost_before_discount"] = "90000.00"
    snapshot(cur_only, tmp_path, coh, None)
    f = by_rec(optimize.summary(cur_only))["coh-sp-data"]
    assert f["phase"] == "needs_evidence" and f["monthly_savings_display"] == "Not estimated"


def test_cost_mismatch_lowers_confidence(cur_only, tmp_path):
    coh = only({"coh-g5-stop"})
    coh[0]["estimated_monthly_cost_after_discount"] = "1000.00"
    snapshot(cur_only, tmp_path, coh, fixtures.co_rows())
    f = by_rec(optimize.summary(cur_only))["coh-g5-stop"]
    assert f["counted"] and f["confidence"] == "low"


# ------------------------------------------------------------------ reproducibility and estimates vs realised

def test_calculation_is_reproducible_from_the_evidence_query(demo_conn):
    f = by_rec(optimize.summary(demo_conn))["coh-g5-stop"]
    q = CostQuery.from_dict(f["evidence_query"])
    total = engine._window_total(demo_conn, q, q.start, q.end)
    assert monthly_run_rate(total, f["days_with_usage"]) == f["evidence_monthly"]
    assert engine.run(demo_conn, q)["status"] == "ok"


def test_every_finding_has_the_required_fields(demo_conn):
    for f in optimize.summary(demo_conn)["opportunities"]:
        assert f["evidence"] and f["time_range"]["start"] < f["time_range"]["end"]
        assert f["calculation"]["steps"] and f["limitations"]
        assert f["owner"] and f["phase_label"] and f["status"] == "open" and f["confidence"]
        if f["phase"] != "needs_evidence":
            assert f["risks"]
            plan = f["action_plan"]
            for key in ("prerequisites", "expected_benefit", "risks", "owner", "validation", "rollback"):
                assert plan[key], key
            assert "estimate" in plan["expected_benefit"].lower()


def test_equal_savings_sort_by_resource(demo_conn):
    ids = [f["resource_id"] for f in optimize.summary(demo_conn)["opportunities"] if f["claim_nanos"] == usd("248.20") and f["counted"]]
    assert len(ids) == 8 and ids == sorted(ids)


def test_owner_comes_from_tag_else_account(demo_conn):
    f = by_rec(optimize.summary(demo_conn))
    assert f["coh-etl-0"]["owner"] == "data-platform"
    assert f["coh-g5-stop"]["owner"] == "sandbox" and "no team tag" in f["coh-g5-stop"]["owner_source"]
    assert any("No team tag" in r for r in f["coh-g5-stop"]["risks"])


def test_estimated_and_realised_stay_separate(demo_conn):
    s = optimize.summary(demo_conn)
    assert s["realised"]["state"] == "not_measured"
    assert all(f["realised"]["state"] == "not_measured" for f in s["opportunities"])
    r = Orchestrator(demo_conn).ask("How much have we saved so far?")
    assert r["kind"] == "optimize" and r["message"].startswith("Realised savings: not measured")


# ------------------------------------------------------------------ money edge cases

def test_run_rate_is_exact_integer_arithmetic():
    assert monthly_run_rate(24 * NANOS_PER_USD, 1) == 730 * NANOS_PER_USD
    assert monthly_run_rate(1, 30) == 1 * 730 // 720
    assert monthly_run_rate(0, 30) == 0
    assert monthly_run_rate(10 ** 18, 0) == 0
    assert isinstance(monthly_run_rate(10 ** 19 + 1, 7), int)


def test_scaled_edges():
    assert scaled(100, 50, 100) == 50
    assert scaled(100, 1, 3) == 33                # rounds down, never overstates
    assert scaled(100, -5, 100) == 0              # removed more than the baseline
    assert scaled(100, 100, 0) == 0               # no baseline
    assert scaled(10 ** 19, 10 ** 19, 10 ** 19) == 10 ** 19


def test_ten_decimal_input_rounds_once_and_sums_exactly(cur_only, tmp_path):
    coh = only({"coh-m6i-4", "coh-m6i-5"})
    for r in coh:
        r["estimated_monthly_savings_after_discount"] = "0.3333333335"
    co = [r for r in fixtures.co_rows() if "i-0c0a0004m6i" in r["instanceArn"] or "i-0c0a0005m6i" in r["instanceArn"]]
    snapshot(cur_only, tmp_path, coh, co)
    t = optimize.summary(cur_only)["totals"]
    each = parse_usd_to_nanos("0.3333333335")
    assert each == 333333334                      # half-even at 9 decimals
    assert t["counted_nanos"] == 2 * each


def test_display_rounding_happens_after_the_sum():
    t = optimize._totals([{"status": "open", "counted": True, "account_id": "a", "resource_id": str(i), "phase": "plan_ready",
                           "savings_nanos": usd("0.005"), "claim_nanos": usd("0.005")} for i in range(3)])
    assert t["counted_nanos"] == usd("0.015") and t["counted_display"] == "$0.02"


def test_savings_equal_to_cost_is_allowed_but_one_nano_more_is_not(cur_only, tmp_path):
    snapshot(cur_only, tmp_path, only({"coh-g5-stop"}), fixtures.co_rows())
    monthly = by_rec(optimize.summary(cur_only))["coh-g5-stop"]["evidence_monthly"]
    worker = DataQualityWorker(cur_only, {})
    cand = {"savings_nanos": monthly, "lookback_days": 14, "action": "Upgrade"}
    facts = {"found": True, "unsupported_rows": 0, "monthly_nanos": monthly}
    assert worker.call("check_evidence", cand=cand, facts=facts, util=None) == []
    cand["savings_nanos"] = monthly + 1
    assert len(worker.call("check_evidence", cand=cand, facts=facts, util=None)) == 1


def test_zero_savings_is_not_a_finding_with_a_number(cur_only, tmp_path):
    coh = only({"coh-ebs-gp3"})
    coh[0]["estimated_monthly_savings_after_discount"] = "0"
    snapshot(cur_only, tmp_path, coh, None)
    assert by_rec(optimize.summary(cur_only))["coh-ebs-gp3"]["phase"] == "needs_evidence"


def test_no_floats_in_money_fields(demo_conn):
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.endswith("_nanos"):
                    assert v is None or isinstance(v, int), k
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(optimize.summary(demo_conn))


# ------------------------------------------------------------------ workers and chat

def test_workers_have_fixed_allow_lists(demo_conn):
    p = optimize.OptimizationPipeline(demo_conn)
    for w in (p.quality, p.investigation, p.optimization, p.risk, p.planning):
        with pytest.raises(PermissionError):
            w.call("run_sql", sql="DROP TABLE line_items")
    with pytest.raises(PermissionError):
        p.planning.call("resource_facts")


def test_pipeline_runs_workers_in_order_and_is_audited(demo_conn):
    s = optimize.summary(demo_conn)
    assert list(s["workers"])[0].startswith("data_quality.")
    assert {k.split(".")[0] for k in s["workers"]} == {"data_quality", "investigation", "optimization", "risk", "action_planning"}
    row = demo_conn.execute("SELECT detail FROM audit_events WHERE event_type='optimize.run' ORDER BY id DESC").fetchone()
    assert json.loads(row["detail"])["state"] == "ok"


def test_running_twice_gives_the_same_result(demo_conn):
    a, b = optimize.summary(demo_conn), optimize.summary(demo_conn)
    assert a["opportunities"] == b["opportunities"] and a["totals"] == b["totals"]


def test_optimize_is_active():
    assert "optimize" in ACTIVE_WORKERS and "optimize" not in PLANNED_WORKERS


@pytest.mark.parametrize("question", [
    "How can I reduce EC2 cost?", "how can I optimize EC2", "any idle resources?", "where is the waste",
    "How much can prod-data save?", "rightsizing opportunities", "lower my AWS bill",
])
def test_savings_questions_route_to_optimize(demo_conn, question):
    assert Orchestrator(demo_conn).ask(question)["kind"] == "optimize"


@pytest.mark.parametrize("question", [
    "Show cost by service for last month", "Show amortized cost by service last month",
    "Why did costs increase in October compared to September?",
])
def test_cost_questions_do_not_route_to_optimize(demo_conn, question):
    assert Orchestrator(demo_conn).ask(question).get("kind") != "optimize"


def test_chat_scope_filters_findings_and_totals(demo_conn):
    r = Orchestrator(demo_conn).ask("How much can prod-data save?")
    assert {f["account"] for f in r["opportunities"]} == {"prod-data"}
    assert r["totals"]["counted_nanos"] == 8 * usd("248.20") + 2 * usd("183.96")


def test_chat_total_matches_page_total(demo_conn):
    r = Orchestrator(demo_conn).ask("How can I optimize my costs?")
    assert r["totals"]["counted_nanos"] == optimize.summary(demo_conn)["totals"]["counted_nanos"]
    assert "$3,272.06" in r["message"] and "estimate" in r["message"].lower()


def test_service_with_no_recommendation_gets_no_invented_number(demo_conn):
    r = Orchestrator(demo_conn).ask("How can I reduce S3 cost?")
    assert r["opportunities"] == [] and "$" not in r["message"]
    assert "no recommendations" in r["message"]


def test_rds_answer_says_evidence_is_missing(demo_conn):
    r = Orchestrator(demo_conn).ask("How can I reduce RDS cost?")
    assert r["totals"]["counted_nanos"] == 0
    assert r["opportunities"][0]["monthly_savings_display"] == "Not estimated"
    assert "evidence" in r["message"]


def test_status_validation(demo_conn):
    with pytest.raises(ValueError):
        optimize.set_status(demo_conn, "0" * 16, "applied")


def test_data_sources_show_snapshot_state(demo_conn):
    from costlens.status import data_status
    states = {s["key"]: s for s in data_status(demo_conn)["sources"]}
    assert states["coh"]["state"] == "connected" and states["compute_optimizer"]["state"] == "connected"
    assert states["storage_lens"]["state"] == "not_connected"


def test_parse_coh_row_keeps_money_as_integers():
    row = parse_coh_row(fixtures.coh_rows()[0])
    assert row["savings_after_nanos"] == parse_usd_to_nanos(str(Decimal("0.0672") * 730))
    assert isinstance(row["savings_after_nanos"], int)
