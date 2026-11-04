"""Savings verification: measuring what billing shows after a recorded change."""
import pytest

from costlens import fixtures, store
from costlens.agent import optimize, verify
from costlens.agent.orchestrator import Orchestrator
from costlens.agent.verify import AttributionWorker, VerificationPipeline, VerificationWorker
from costlens.money import parse_usd_to_nanos as usd
from costlens.query import engine
from costlens.query.model import CostQuery


def results(conn) -> dict:
    return {r["recommendation_id"]: r for r in VerificationPipeline(conn).run()["results"]}


def finding(conn, rec_id: str) -> dict:
    return next(f for f in optimize.summary(conn)["opportunities"] if f["recommendation_id"] == rec_id)


def rec_ids(conn) -> dict:
    return {f["recommendation_id"]: f["id"] for f in optimize.summary(conn)["opportunities"]}


# ------------------------------------------------------------------ the planted outcomes

@pytest.mark.parametrize("rec_id,state", [
    ("coh-g5-stop", "verified"), ("coh-etl-0", "verified"), ("coh-etl-2", "not_realised"), ("coh-m6i-0", "not_realised"),
    ("coh-etl-3", "cannot_verify"), ("coh-r6i-0", "partly_realised"), ("coh-etl-grav-0", "partly_realised"),
    ("coh-ebs-gp3", "provisional"), ("coh-m6i-4", "waiting"),
])
def test_scenario_outcomes(savings_conn, rec_id, state):
    assert results(savings_conn)[rec_id]["state"] == state


def test_stop_matches_hand_calculation(savings_conn):
    r = results(savings_conn)["coh-g5-stop"]
    assert r["realised_nanos"] == usd("1.006") * 24 * 26
    assert r["monthly_display"] == "$734.38" and r["after_window"]["days"] == 26


def test_rightsize_matches_hand_calculation(savings_conn):
    r = results(savings_conn)["coh-etl-0"]
    assert r["realised_nanos"] == usd("0.340") * 24 * 10
    assert r["monthly_display"] == "$248.20"


def test_totals_add_only_final_measured_results(savings_conn):
    v = VerificationPipeline(savings_conn).run()
    by = {r["recommendation_id"]: r for r in v["results"]}
    expected = sum(by[k]["realised_nanos"] for k in ("coh-g5-stop", "coh-etl-0", "coh-etl-2", "coh-m6i-0", "coh-r6i-0", "coh-etl-grav-0"))
    assert v["totals"]["final_nanos"] == expected
    assert v["totals"]["provisional_nanos"] == by["coh-ebs-gp3"]["realised_nanos"]
    assert v["totals"]["counts"] == {"waiting": 1, "provisional": 1, "verified": 2, "partly_realised": 2, "not_realised": 2, "cannot_verify": 1}


# ------------------------------------------------------------------ usage is not a saving

def test_usage_drop_gets_no_credit(savings_conn):
    r = results(savings_conn)["coh-etl-2"]
    assert r["realised_nanos"] == 0
    usage = next(x for x in r["split"] if x["label"] == "Usage effect")
    assert usage["value"] == "$171.36" and not usage["counted"]
    assert "usage fell" in r["reason"]


def test_usage_rise_does_not_hide_a_real_saving():
    a = AttributionWorker(None, {})
    from fractions import Fraction
    base = {"cost_nanos": usd("100"), "usage": Fraction(100)}
    after = {"cost_nanos": usd("75"), "usage": Fraction(150)}  # 50% more hours at half the rate
    s = a.call("split", base=base, after=after, base_days=10, after_days=10)
    assert s["total_change"] == usd("25") and s["rate_effect"] == usd("75") and s["usage_effect"] == -usd("50")
    assert s["rate_effect"] + s["usage_effect"] == s["total_change"]


def test_effects_always_add_up_to_the_total_change():
    from fractions import Fraction
    a = AttributionWorker(None, {})
    s = a.call("split", base={"cost_nanos": 1000000007, "usage": Fraction(7, 3)},
               after={"cost_nanos": 333333331, "usage": Fraction(11, 7)}, base_days=14, after_days=9)
    assert s["usage_effect"] + s["rate_effect"] == s["total_change"]
    assert all(isinstance(s[k], int) for k in s)


def test_stop_gets_credit_for_the_usage_drop(savings_conn):
    r = results(savings_conn)["coh-g5-stop"]
    assert next(x for x in r["split"] if x["label"] == "Usage effect")["counted"]


# ------------------------------------------------------------------ commitment

def test_unused_commitment_reduces_the_saving_to_zero(savings_conn):
    r = results(savings_conn)["coh-m6i-0"]
    split = {x["label"]: x["value"] for x in r["split"]}
    assert split["Rate effect"] == "$33.87" and split["Commitment effect"] == "-$33.87"
    assert r["realised_nanos"] < usd("0.01") and "freed commitment went unused" in r["reason"]


def test_commitment_growth_is_shared_out_once():
    a = AttributionWorker(None, {})
    first = a.call("commitment_effect", unused_base=usd("14"), unused_after=usd("24"), base_days=14, after_days=8, claimed_daily=0)
    assert first["daily"] == usd("2")
    second = a.call("commitment_effect", unused_base=usd("14"), unused_after=usd("24"), base_days=14, after_days=8, claimed_daily=first["daily"])
    assert second["total"] == 0


def test_lower_unused_commitment_gives_no_deduction():
    a = AttributionWorker(None, {})
    assert a.call("commitment_effect", unused_base=usd("28"), unused_after=usd("7"), base_days=14, after_days=7, claimed_daily=0)["total"] == 0


def test_uncovered_resource_has_no_commitment_effect(savings_conn):
    r = results(savings_conn)["coh-etl-0"]
    assert next(x for x in r["split"] if x["label"] == "Commitment effect")["value"] in ("$0.00", "-$0.00")


# ------------------------------------------------------------------ overlaps

def test_two_changes_on_one_resource_never_share_days(savings_conn):
    by = results(savings_conn)
    first, second = by["coh-etl-0"], by["coh-etl-grav-0"]
    assert first["after_window"]["end"] == "2026-11-19"
    assert second["baseline_window"]["start"] == "2026-11-10" and second["after_window"]["start"] == "2026-11-20"


def test_second_change_is_measured_from_the_first_ones_rate(savings_conn):
    r = results(savings_conn)["coh-etl-grav-0"]
    assert r["realised_nanos"] == (usd("0.340") - usd("0.290")) * 24 * 11  # not from the original 0.680


def test_same_day_changes_on_one_resource_are_refused(demo_conn):
    ids = rec_ids(demo_conn)
    verify.mark_implemented(demo_conn, ids["coh-etl-0"], "2026-10-25")
    with pytest.raises(ValueError, match="same date"):
        verify.mark_implemented(demo_conn, ids["coh-etl-grav-0"], "2026-10-25")


# ------------------------------------------------------------------ states and missing data

def test_waiting_gives_no_number(savings_conn):
    r = results(savings_conn)["coh-m6i-4"]
    assert r["realised_nanos"] is None and r["monthly_display"] == "No number" and "4 days" in r["reason"]


def test_replaced_resource_gives_no_number(savings_conn):
    r = results(savings_conn)["coh-etl-3"]
    assert r["realised_nanos"] is None and "replaced" in r["reason"]


def test_not_final_billing_is_provisional_and_kept_apart(savings_conn):
    r = results(savings_conn)["coh-ebs-gp3"]
    assert not r["final"] and r["realised_nanos"] > 0 and "not final" in r["reason"]


def test_final_billing_is_preferred_over_the_latest_days(savings_conn):
    r = results(savings_conn)["coh-g5-stop"]
    assert r["final"] and r["after_window"]["end"] == "2026-11-30"


def test_expected_change_missing_from_cur_is_not_verified(savings_conn):
    r = results(savings_conn)["coh-r6i-0"]
    assert not r["expected_seen"] and any("does not show in CUR" in x for x in r["limitations"])


def test_missing_month_in_the_baseline_says_so(savings_conn):
    savings_conn.execute("UPDATE deliveries SET status='superseded' WHERE billing_period='2026-10'")
    savings_conn.execute("DELETE FROM line_items WHERE billing_period='2026-10'")
    r = results(savings_conn)["coh-g5-stop"]
    assert r["state"] == "cannot_verify" and "2026-10" in r["reason"] and r["realised_nanos"] is None


def test_short_baseline_gives_no_number(savings_conn):
    savings_conn.execute("DELETE FROM line_items WHERE resource_id='i-0f0a0001g5' AND usage_date < '2026-11-01'")
    r = results(savings_conn)["coh-g5-stop"]
    assert r["state"] == "cannot_verify" and "baseline" in r["reason"]


def test_unsupported_charges_give_no_number(savings_conn):
    savings_conn.execute("UPDATE line_items SET amortized_nanos=NULL WHERE resource_id='i-0f0a0001g5' AND usage_date='2026-11-01'")
    r = results(savings_conn)["coh-g5-stop"]
    assert r["state"] == "cannot_verify" and r["realised_nanos"] is None


def test_billing_revision_changes_the_result(savings_conn):
    before = results(savings_conn)["coh-etl-0"]["realised_nanos"]
    savings_conn.execute("UPDATE line_items SET amortized_nanos=amortized_nanos + 1000000000 "
                         "WHERE resource_id='i-0d0e0000c6i' AND usage_date='2026-11-12'")
    assert results(savings_conn)["coh-etl-0"]["realised_nanos"] == before - 1000000000


def test_credits_are_shown_but_not_counted(savings_conn):
    before = results(savings_conn)["coh-etl-0"]["realised_nanos"]
    row = dict(savings_conn.execute("SELECT * FROM line_items WHERE resource_id='i-0d0e0000c6i' AND usage_date='2026-11-12'").fetchone())
    row.update(line_item_id="credit-1", line_item_type="Credit", unblended_nanos=-usd("50"), amortized_nanos=-usd("50"), usage_amount="0")
    savings_conn.execute(f"INSERT INTO line_items({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    r = results(savings_conn)["coh-etl-0"]
    assert r["realised_nanos"] == before
    credit = next(x for x in r["split"] if x["label"] == "Credits and refunds")
    assert credit["value"] == "-$50.00" and not credit["counted"]


@pytest.mark.parametrize("monthly,seen,final,state", [
    (100, True, True, "verified"), (80, True, True, "verified"), (79, True, True, "partly_realised"),
    (5, True, True, "partly_realised"), (4, True, True, "not_realised"), (0, True, True, "not_realised"),
    (-10, True, True, "not_realised"), (100, False, True, "partly_realised"), (100, True, False, "provisional"),
    (0, True, False, "provisional"),
])
def test_judge_boundaries(monthly, seen, final, state):
    assert VerificationWorker(None, {}).call("judge", estimate=100, monthly=monthly, expected_seen=seen, final=final) == state


# ------------------------------------------------------------------ estimates stay separate

def test_nothing_recorded_stays_not_measured(demo_conn):
    s = optimize.summary(demo_conn)
    assert s["realised"]["state"] == "not_measured" and s["totals"]["realised_display"] == "Not measured"
    assert s["verification"]["results"] == []


def test_implemented_finding_leaves_the_open_estimate(demo_conn):
    ids = rec_ids(demo_conn)
    verify.mark_implemented(demo_conn, ids["coh-g5-stop"], "2026-10-20")
    s = optimize.summary(demo_conn)
    assert s["totals"]["counted_nanos"] == usd("3272.06") - usd("734.38")
    assert finding(demo_conn, "coh-g5-stop")["status"] == "implemented"


def test_undo_restores_the_estimate(demo_conn):
    ids = rec_ids(demo_conn)
    verify.mark_implemented(demo_conn, ids["coh-g5-stop"], "2026-10-20")
    assert verify.undo(demo_conn, ids["coh-g5-stop"]) and not verify.undo(demo_conn, ids["coh-g5-stop"])
    assert optimize.summary(demo_conn)["totals"]["counted_nanos"] == usd("3272.06")


def test_realised_and_estimates_are_never_added(savings_conn):
    s = optimize.summary(savings_conn)
    assert s["totals"]["realised_display"] == s["verification"]["totals"]["final_display"]
    open_estimates = sum(f["savings_nanos"] for f in s["opportunities"] if f["counted"] and f["status"] != "implemented")
    assert s["totals"]["counted_nanos"] == open_estimates  # no realised money and no implemented finding inside it
    assert "separately" in s["realised"]["detail"]


def test_estimate_is_frozen_when_the_change_is_recorded(savings_conn):
    assert results(savings_conn)["coh-g5-stop"]["estimate_nanos"] == usd("734.38")
    savings_conn.execute("DELETE FROM coh_recommendations")
    assert results(savings_conn)["coh-g5-stop"]["estimate_nanos"] == usd("734.38")


def test_finding_shows_its_result(savings_conn):
    f = next(x for x in optimize.summary(savings_conn)["opportunities"] if x["recommendation_id"] == "coh-etl-0")
    assert f["realised"]["state"] == "verified" and "Verified" in f["realised"]["display"]


# ------------------------------------------------------------------ recording

@pytest.mark.parametrize("day,phrase", [("soon", "YYYY-MM-DD"), ("2027-01-01", "future")])
def test_bad_dates_are_refused(demo_conn, day, phrase):
    with pytest.raises(ValueError, match=phrase):
        verify.mark_implemented(demo_conn, rec_ids(demo_conn)["coh-g5-stop"], day)


def test_unknown_and_repeated_findings_are_refused(demo_conn):
    with pytest.raises(ValueError, match="not found"):
        verify.mark_implemented(demo_conn, "0" * 16, "2026-10-20")
    gid = rec_ids(demo_conn)["coh-g5-stop"]
    verify.mark_implemented(demo_conn, gid, "2026-10-20")
    with pytest.raises(ValueError, match="already"):
        verify.mark_implemented(demo_conn, gid, "2026-10-21")


@pytest.mark.parametrize("rec_id", ["coh-gone", "coh-sp-data"])
def test_needs_evidence_and_commitments_cannot_be_recorded(demo_conn, rec_id):
    with pytest.raises(ValueError, match="cannot be measured"):
        verify.mark_implemented(demo_conn, rec_ids(demo_conn)[rec_id], "2026-10-20")


# ------------------------------------------------------------------ reproducibility, chat, workers

def test_after_cost_reproduces_from_its_query(savings_conn):
    r = results(savings_conn)["coh-etl-0"]
    q = CostQuery.from_dict(r["after_query"])
    assert engine._window_total(savings_conn, q, q.start, q.end) == usd("0.340") * 24 * 10
    assert engine.run(savings_conn, q)["status"] == "ok"


def test_no_floats_in_realised_numbers(savings_conn):
    for r in results(savings_conn).values():
        for k in ("realised_nanos", "monthly_nanos", "estimate_nanos"):
            assert r[k] is None or isinstance(r[k], int)


def test_results_are_recalculated_not_stored(savings_conn):
    cols = [c["name"] for c in savings_conn.execute("PRAGMA table_info(opt_changes)")]
    assert not [c for c in cols if "realised" in c]


def test_workers_have_fixed_allow_lists(savings_conn):
    p = VerificationPipeline(savings_conn)
    for w in (p.records, p.baseline, p.attribution, p.verification):
        with pytest.raises(PermissionError):
            w.call("run_sql", sql="x")


def test_chat_uses_measured_results(savings_conn):
    r = Orchestrator(savings_conn).ask("How much have we saved so far?")
    t = optimize.summary(savings_conn)["verification"]["totals"]
    assert r["message"].startswith("Realised so far:") and t["final_display"] in r["message"]
    assert "1 cannot be verified" in r["message"] and "1 waiting" in r["message"]


def test_scenario_is_separate_from_the_main_demo(demo_conn):
    assert demo_conn.execute("SELECT COUNT(*) FROM opt_changes").fetchone()[0] == 0
    assert engine.reference_date(demo_conn) == fixtures.REFERENCE_DATE


def test_scenario_reload_is_safe(savings_conn, fixture_root):
    outcomes = verify.load_savings_scenario(savings_conn, fixture_root)
    assert {o.status for o in outcomes} == {"duplicate"}
    assert savings_conn.execute("SELECT COUNT(*) FROM opt_changes").fetchone()[0] == len(fixtures.savings_changes())


def test_scenario_audit_records_runs(savings_conn):
    VerificationPipeline(savings_conn).run()
    assert savings_conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='verify.run'").fetchone()[0] >= 1
    assert store.get_meta(savings_conn, "reference_date") == fixtures.SAVINGS_REFERENCE_DATE.isoformat()
