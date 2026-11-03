from datetime import date

import pytest

from costlens.agent.interpreter import DeterministicInterpreter, Vocabulary
from costlens.query.model import CostQuery

TODAY = date(2026, 11, 3)
VOCAB = Vocabulary(
    services=["AmazonEC2", "AmazonS3", "AmazonRDS", "AWSLambda", "AWSGlue", "ComputeSavingsPlans"],
    accounts={"210987654321": "prod-commerce", "310987654321": "prod-data", "410987654321": "staging",
              "510987654321": "sandbox"},
    regions=["us-east-1", "eu-west-1", "ap-south-1", "global"],
    usage_types=["NatGateway-Bytes", "NatGateway-Hours", "BoxUsage:m6i.xlarge"],
    tags={"user:team": ["checkout", "data-platform"], "user:env": ["dev", "prod", "staging"],
          "user:app": ["checkout-api", "etl-batch"]},
)
I = DeterministicInterpreter()


def ask(text, prev=None):
    return I.interpret(text, VOCAB, TODAY, prev)


def test_last_month_by_service():
    r = ask("Show cost by service for last month")
    assert r.status == "ok"
    assert (r.query.start, r.query.end) == (date(2026, 10, 1), date(2026, 11, 1))
    assert r.query.group_by == "service" and r.query.kind == "explore"


def test_variance_two_months():
    r = ask("Why did costs increase in October compared to September?")
    q = r.query
    assert q.kind == "variance"
    assert (q.start, q.compare_start) == (date(2026, 10, 1), date(2026, 9, 1))


def test_filters_and_grouping():
    q = ask("Daily EC2 cost by account for the last 30 days").query
    assert q.filters == {"service": ["AmazonEC2"]} and q.group_by == "account" and q.granularity == "daily"
    assert (q.end - q.start).days == 30


def test_account_name_not_matched_as_env_tag():
    q = ask("NAT gateway spend in prod data by day").query
    assert q.filters.get("account") == ["310987654321"]
    assert "tag:user:env" not in q.filters
    assert set(q.filters["usage_type"]) == {"NatGateway-Bytes", "NatGateway-Hours"}


def test_staging_prefers_account():
    q = ask("staging cost in october").query
    assert q.filters == {"account": ["410987654321"]}


def test_region_alias_and_amortized_top_n():
    q = ask("top 3 teams by amortized cost in mumbai this month").query
    assert q.filters == {"region": ["ap-south-1"]} and q.metric == "amortized" and q.top_n == 3
    assert q.group_by == "tag:user:team" and q.start == date(2026, 11, 1)


def test_follow_up_inherits_previous():
    prev = ask("Daily EC2 cost by account for the last 30 days").query
    q = ask("now by region", prev).query
    assert q.group_by == "region" and q.filters == prev.filters and (q.start, q.end) == (prev.start, prev.end)
    q2 = ask("only prod-data", q).query
    assert q2.filters["account"] == ["310987654321"] and q2.filters["service"] == ["AmazonEC2"]
    q3 = ask("why did it change", prev).query
    assert q3.kind == "variance" and (q3.start, q3.end) == (prev.start, prev.end)
    assert (q3.compare_end - q3.compare_start) == (prev.end - prev.start)


def test_clear_filters():
    prev = ask("Daily EC2 cost by account for the last 30 days").query
    assert ask("show all services", prev).query.filters == {}


@pytest.mark.parametrize("text,status,module", [
    ("hello there", "clarify", "visualize"),
    ("", "clarify", "visualize"),
    ("forecast next month", "unsupported", "visualize"),
    ("how can I optimize EC2", "unsupported", "optimize"),
    ("set a budget for prod", "unsupported", "govern"),
    ("show azure cost", "unsupported", "visualize"),
])
def test_refusals(text, status, module):
    r = ask(text)
    assert r.status == status and r.module == module and r.query is None


def test_may_as_verb_is_not_a_month():
    q = ask("what may have caused cost by service").query
    assert q.start != date(2026, 5, 1)


def test_assumptions_are_reported():
    r = ask("cost by account")
    assert any("last 30 days" in a for a in r.assumptions)


def test_query_round_trips():
    q = ask("Why did costs increase in October compared to September?").query
    assert CostQuery.from_dict(q.to_dict()) == q
