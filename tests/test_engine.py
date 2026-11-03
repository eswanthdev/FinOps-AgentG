from datetime import date

import pytest

from costlens.query import engine
from costlens.query.model import CostQuery, QueryError


def q(**kw):
    base = dict(start=date(2026, 10, 1), end=date(2026, 11, 1))
    base.update(kw)
    return CostQuery(**base)


def cents(s: str) -> int:
    return int(s.replace(".", "").replace("-", "")) * (-1 if s.startswith("-") else 1)


def db_total(conn, start, end, col="unblended_nanos", extra="", params=()):
    return conn.execute(f"SELECT SUM({col}) FROM line_items WHERE usage_date>=? AND usage_date<? {extra}",
                        (start, end, *params)).fetchone()[0]


def test_explore_total_matches_line_items(demo_conn):
    r = engine.run(demo_conn, q())
    assert r["status"] == "ok"
    from costlens.money import nanos_to_cents_str
    assert r["total"] == nanos_to_cents_str(db_total(demo_conn, "2026-10-01", "2026-11-01"))
    assert r["days_with_data"] == 31


def test_top_n_other_preserves_total(demo_conn):
    full = engine.run(demo_conn, q(granularity="total", top_n=20))
    short = engine.run(demo_conn, q(granularity="total", top_n=3))
    assert short["series"][-1]["name"] == "Other"
    assert len(short["series"]) == 4
    assert short["total"] == full["total"]
    assert sum(cents(s["total"]) for s in short["series"]) == cents(short["total"])


def test_bucket_totals_add_up(demo_conn):
    r = engine.run(demo_conn, q(granularity="daily", group_by="account"))
    assert len(r["buckets"]) == 31
    assert abs(sum(r["bucket_totals_cents"]) - cents(r["total"])) <= len(r["buckets"])  # per-bucket display rounding only


def test_amortized_equals_unblended_in_total_but_moves_cost(demo_conn):
    u = engine.run(demo_conn, q(granularity="total", group_by="service", top_n=20))
    a = engine.run(demo_conn, q(granularity="total", group_by="service", top_n=20, metric="amortized"))
    assert u["total"] == a["total"]  # commitment fully accounted either way
    us = {s["key"]: s["total"] for s in u["series"]}
    as_ = {s["key"]: s["total"] for s in a["series"]}
    assert cents(as_["AmazonEC2"]) > cents(us["AmazonEC2"])  # covered usage now carries its effective cost
    assert cents(as_["ComputeSavingsPlans"]) < cents(us["ComputeSavingsPlans"])  # only unused commitment remains


def test_amortized_refuses_unsupported_charge_types(demo_conn):
    demo_conn.execute("UPDATE line_items SET line_item_type='RIFee', amortized_nanos=NULL WHERE rowid IN "
                      "(SELECT rowid FROM line_items WHERE usage_date='2026-10-10' LIMIT 1)")
    r = engine.run(demo_conn, q(metric="amortized"))
    assert r["status"] == "insufficient_data" and "cannot amortize" in r["reason"]
    assert engine.run(demo_conn, q(metric="unblended"))["status"] == "ok"


def test_missing_period_is_insufficient_not_zero(demo_conn):
    r = engine.run(demo_conn, q(start=date(2026, 7, 1), end=date(2026, 8, 1)))
    assert r["status"] == "insufficient_data"
    assert r["coverage"]["periods"][0]["status"] == "missing"


def test_partial_coverage_warns_and_trims(demo_conn):
    r = engine.run(demo_conn, q(start=date(2026, 7, 15), end=date(2026, 8, 15)))
    assert r["status"] == "ok"
    assert r["effective_start"] == "2026-08-01"
    assert any("2026-07" in w for w in r["warnings"])
    assert r["comparison"] is None and "not fully covered" in r["comparison_note"]


def test_future_days_trimmed_and_flagged(demo_conn):
    r = engine.run(demo_conn, q(start=date(2026, 11, 1), end=date(2026, 12, 1)))
    assert r["effective_end"] == "2026-11-03"
    assert any("not final" in w for w in r["warnings"])
    assert any("only up to" in w for w in r["warnings"])


def test_rejected_newer_delivery_is_reported(demo_conn):
    cov = engine.coverage(demo_conn, date(2026, 11, 1), date(2026, 11, 3))
    assert "newer_delivery_rejected" in cov["periods"][0]


def test_tag_grouping_and_untagged_filter(demo_conn):
    r = engine.run(demo_conn, q(granularity="total", group_by="tag:user:team"))
    names = [s["name"] for s in r["series"]]
    assert "(untagged)" in names and "data-platform" in names
    u = engine.run(demo_conn, q(granularity="total", group_by="account", filters={"tag:user:team": ["(untagged)"]}))
    untagged = next(s for s in r["series"] if s["name"] == "(untagged)")
    assert u["total"] == untagged["total"]


def test_filter_values_are_parameters_not_sql(demo_conn):
    r = engine.run(demo_conn, q(filters={"service": ["AmazonEC2'); DROP TABLE line_items; --"]}))
    assert r["status"] == "ok" and r["total"] == "0.00"
    assert demo_conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] > 0


@pytest.mark.parametrize("bad", [
    dict(group_by="service; DROP TABLE x"),
    dict(group_by='tag:x") OR 1=1 --'),
    dict(filters={"resource_tags": ["x"]}),
    dict(metric="blended"),
    dict(top_n=0),
    dict(start=date(2026, 1, 1), end=date(2027, 6, 1)),
    dict(end=date(2026, 10, 1)),
    dict(kind="variance"),
])
def test_invalid_queries_rejected(bad):
    with pytest.raises(QueryError):
        q(**bad).validate()


def test_from_dict_rejects_unknown_fields():
    with pytest.raises(QueryError):
        CostQuery.from_dict({"start": "2026-10-01", "end": "2026-11-01", "sql": "SELECT 1"})


def test_variance_finds_new_fleet_and_sums(demo_conn):
    r = engine.run(demo_conn, q(kind="variance", compare_start=date(2026, 9, 1), compare_end=date(2026, 10, 1),
                                group_by="service"))
    assert r["status"] == "ok"
    # Exact equality is asserted inside the engine in nano-USD; per-row cents may differ by display rounding only.
    diff = sum(x["delta_cents"] for x in r["rows"]) - (cents(r["current"]["total"]) - cents(r["baseline"]["total"]))
    assert abs(diff) <= len(r["rows"])
    top = r["drivers"]["groups"][0]
    assert top["usage_type"] == "BoxUsage:c6i.4xlarge" and top["new_resources"] == 8
    assert top["first_seen"] == "2026-10-15"
    assert any("different lengths" in w for w in r["warnings"])


def test_variance_with_missing_baseline_is_insufficient(demo_conn):
    r = engine.run(demo_conn, q(kind="variance", start=date(2026, 8, 1), end=date(2026, 9, 1),
                                compare_start=date(2026, 7, 1), compare_end=date(2026, 8, 1), group_by="service"))
    assert r["status"] == "insufficient_data" and "baseline" in r["reason"]


def test_revision_credit_visible_in_september(demo_conn):
    r = engine.run(demo_conn, q(start=date(2026, 9, 1), end=date(2026, 10, 1), granularity="total",
                                group_by="line_item_type", top_n=20))
    credit = next(s for s in r["series"] if s["key"] == "Credit")
    assert credit["total"] == "-250.00"
    assert any("credits" in w for w in r["warnings"])
