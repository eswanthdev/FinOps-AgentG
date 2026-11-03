from costlens import anomalies


def test_detects_planted_anomalies_only(demo_conn):
    found = anomalies.detect(demo_conn)
    keys = {(a["account_id"], a["service"], a["start"]) for a in found}
    assert keys == {("310987654321", "AmazonEC2", "2026-10-15"), ("510987654321", "AmazonEC2", "2026-09-20")}


def test_anomaly_evidence(demo_conn):
    a = next(x for x in anomalies.detect(demo_conn) if x["start"] == "2026-10-15")
    assert a["type"] == "sustained" and a["new_normal"] is True and not a["ongoing"]
    assert a["owner"] == "data-platform" and "tag" in a["owner_source"]
    assert "8 new resource(s)" in a["explanation"]
    # impact is summed in nano-USD; per-day cents can differ only by display rounding
    assert abs(a["impact_cents"] - sum(d["actual_cents"] - d["expected_cents"] for d in a["series"])) <= 2 * a["days"]
    assert a["baseline_window"] == ["2026-10-01", "2026-10-14"]
    assert len(a["context"]["days"]) == len(a["context"]["actual_cents"]) == len(a["context"]["flagged"])


def test_owner_falls_back_to_account(demo_conn):
    a = next(x for x in anomalies.detect(demo_conn) if x["start"] == "2026-09-20")
    assert a["owner"] == "sandbox" and "account" in a["owner_source"]


def test_status_changes_persist_and_validate(demo_conn):
    a = anomalies.detect(demo_conn)[0]
    anomalies.set_status(demo_conn, a["id"], "dismissed", "planned ETL launch")
    again = next(x for x in anomalies.detect(demo_conn) if x["id"] == a["id"])
    assert again["status"] == "dismissed" and again["note"] == "planned ETL launch"
    assert anomalies.summary(demo_conn)["open_count"] == 1
    import pytest
    with pytest.raises(ValueError):
        anomalies.set_status(demo_conn, a["id"], "deleted")
    with pytest.raises(ValueError):
        anomalies.set_status(demo_conn, "' OR 1=1 --", "open")


def test_no_anomalies_on_empty_store(fresh_conn):
    assert anomalies.detect(fresh_conn) == []


def test_single_day_spike_is_a_spike(demo_conn):
    demo_conn.execute("UPDATE line_items SET unblended_nanos = unblended_nanos * 5 "
                      "WHERE account_id='410987654321' AND service='AmazonRDS' AND usage_date='2026-08-25'")
    a = next(x for x in anomalies.detect(demo_conn) if x["account_id"] == "410987654321")
    assert a["type"] == "spike" and a["days"] == 1 and a["start"] == "2026-08-25"
