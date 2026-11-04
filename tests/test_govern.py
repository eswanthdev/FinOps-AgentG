"""Governance: policies, violations, impact, drafts, workflow and exceptions."""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from costlens import fixtures, store
from costlens.agent import govern
from costlens.agent.govern import check_document
from costlens.agent.orchestrator import ACTIVE_WORKERS, Orchestrator, PLANNED_WORKERS
from costlens.ingest.governance import GovernanceError, PolicySpec, load_governance, validate_organization
from costlens.ingest.loader import ingest_source
from costlens.ingest.sources import LocalDirectorySource
from costlens.money import parse_usd_to_nanos as usd
from costlens.query import engine
from costlens.query.model import CostQuery
from costlens.status import data_status

SANDBOX = "510987654321"


def by_title(items: list[dict], text: str) -> dict:
    return next(i for i in items if text in i["title"])


def gpu_draft(conn) -> dict:
    return by_title(govern.summary(conn)["guardrails"], "No GPU")


def write(tmp_path: Path, org=None, policies=None) -> Path:
    root = tmp_path / "gov"
    root.mkdir(exist_ok=True)
    if org is not None:
        (root / "organization.json").write_text(json.dumps(org), encoding="utf-8")
    if policies is not None:
        (root / "policies.json").write_text(json.dumps(policies), encoding="utf-8")
    return root


@pytest.fixture()
def cur_only(fresh_conn, fixture_root):
    ingest_source(fresh_conn, LocalDirectorySource(fixture_root))
    store.set_meta(fresh_conn, "reference_date", fixtures.REFERENCE_DATE.isoformat())
    return fresh_conn


# ------------------------------------------------------------------ policy validation

@pytest.mark.parametrize("policy,phrase", [
    ({"id": "a", "name": "x", "kind": "run_sql", "params": {}}, "unsupported policy kind"),
    ({"id": "a", "name": "x", "kind": "required_tag", "params": {"key": "team", "sql": "x"}}, "params must be exactly"),
    ({"id": "a", "name": "x", "kind": "required_tag", "params": {}}, "params must be exactly"),
    ({"id": "a", "name": "x", "kind": "required_tag", "params": {"key": "bad key!"}}, "key is invalid"),
    ({"id": "a", "name": "x", "kind": "allowed_regions", "params": {"regions": ["mars-1"]}}, "invalid value"),
    ({"id": "a", "name": "x", "kind": "allowed_regions", "params": {"regions": []}}, "needs 1 to"),
    ({"id": "a", "name": "x", "kind": "restricted_instance", "params": {"accounts": ["123"], "families": ["g5"]}}, "invalid value"),
    ({"id": "a", "name": "x", "kind": "restricted_instance", "params": {"accounts": [SANDBOX], "families": ["g5*"]}}, "invalid value"),
    ({"id": "A B", "name": "x", "kind": "budget_coverage", "params": {}}, "policy id"),
    ({"id": "a", "name": "", "kind": "budget_coverage", "params": {}}, "policy name"),
    ({"id": "a", "name": "x", "kind": "budget_coverage", "params": {}, "extra": 1}, "unknown policy fields"),
])
def test_bad_policies_are_rejected(policy, phrase):
    with pytest.raises(GovernanceError, match=phrase):
        PolicySpec.from_dict(policy)


def test_bad_policy_file_keeps_previous_policies(demo_conn, tmp_path):
    root = write(tmp_path, policies={"schemaVersion": "policy-subset-v1", "policies": [{"id": "a", "name": "x", "kind": "nope", "params": {}}]})
    assert load_governance(demo_conn, root)[0].status == "rejected_invalid"
    assert demo_conn.execute("SELECT COUNT(*) FROM gov_policies").fetchone()[0] == 4


@pytest.mark.parametrize("change,phrase", [
    (lambda o: o.update(schemaVersion="x"), "schemaVersion"),
    (lambda o: o.update(managementAccount="1"), "managementAccount"),
    (lambda o: o["organizationalUnits"][0]["accounts"].append("510987654321"), "two organizational units"),
    (lambda o: o["serviceControlPolicies"][0]["targets"].append("ou-none"), "not in this organization"),
])
def test_bad_organization_is_rejected(change, phrase):
    org = fixtures.organization()
    change(org)
    with pytest.raises(GovernanceError, match=phrase):
        validate_organization(org)


def test_invalid_json_is_rejected(cur_only, tmp_path):
    root = write(tmp_path)
    (root / "organization.json").write_text("{not json", encoding="utf-8")
    assert load_governance(cur_only, root)[0].status == "rejected_invalid"


def test_reload_is_duplicate_and_keeps_the_on_off_choice(demo_conn, fixture_root):
    govern.set_policy_enabled(demo_conn, "team-tag", False)
    assert load_governance(demo_conn, fixture_root / fixtures.GOV_FOLDER)[0].status == "duplicate"
    assert demo_conn.execute("SELECT enabled FROM gov_policies WHERE id='team-tag'").fetchone()[0] == 0


# ------------------------------------------------------------------ violations

def test_demo_violations(demo_conn):
    s = govern.summary(demo_conn)
    assert s["state"] == "ok" and s["totals"]["open_count"] == 6
    assert {p["id"]: p["result"] for p in s["policies"]} == {
        "team-tag": "violations", "approved-regions": "compliant", "no-gpu-sandbox": "violations",
        "budget-every-account": "violations"}


def test_required_tag_finds_only_untagged_sandbox_resources(demo_conn):
    v = by_title(govern.summary(demo_conn)["violations"], "team tag")
    assert v["account"] == "sandbox" and v["resource_count"] == 4
    assert {r["resource_id"] for r in v["resources"]} == {"i-0f0a0001g5", "i-0f0b0000t3", "i-0f0b0001t3", "sandbox-experiments"}
    assert not v["owner_known"] and v["owner"] == "sandbox"


def test_resource_spend_adds_up_to_the_violation(demo_conn):
    for v in govern.summary(demo_conn)["violations"]:
        assert sum(r["spend_nanos"] for r in v["resources"]) == v["spend_nanos"]


def test_violation_spend_reproduces_from_its_query(demo_conn):
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    q = CostQuery.from_dict(v["evidence_query"])
    assert engine._window_total(demo_conn, q, q.start, q.end) == v["spend_nanos"]
    assert engine.run(demo_conn, q)["status"] == "ok"
    assert v["spend_display"] == "$724.32"


def test_total_counts_each_resource_once(demo_conn):
    s = govern.summary(demo_conn)
    gpu, tag = by_title(s["violations"], "GPU"), by_title(s["violations"], "team tag")
    assert "i-0f0a0001g5" in {r["resource_id"] for r in gpu["resources"]} & {r["resource_id"] for r in tag["resources"]}
    assert s["totals"]["spend_nanos"] == tag["spend_nanos"]  # the GPU is not added twice
    assert s["totals"]["spend_display"] == "$808.07"


def test_budget_gaps_are_not_added_to_the_spend_total(demo_conn):
    s = govern.summary(demo_conn)
    gaps = [v for v in s["violations"] if v["kind"] == "budget_coverage"]
    assert len(gaps) == 4 and s["totals"]["spend_nanos"] < sum(v["spend_nanos"] for v in gaps)
    assert all("does not choose the amount" in v["suggestion"] for v in gaps)


def budget(conn, filters: dict) -> None:
    conn.execute("INSERT INTO budgets(name, spec_json, created_at) VALUES ('b', ?, 'now')", (json.dumps({"filters": filters}),))


def test_account_budget_closes_the_gap_for_that_account_only(demo_conn):
    budget(demo_conn, {"account": [SANDBOX]})
    gaps = [v for v in govern.summary(demo_conn)["violations"] if v["kind"] == "budget_coverage"]
    assert len(gaps) == 3 and SANDBOX not in {v["account_id"] for v in gaps}


def test_all_spend_budget_closes_every_gap(demo_conn):
    budget(demo_conn, {})
    assert not [v for v in govern.summary(demo_conn)["violations"] if v["kind"] == "budget_coverage"]


def test_service_budget_does_not_count_as_account_coverage(demo_conn):
    budget(demo_conn, {"account": [SANDBOX], "service": ["AmazonEC2"]})
    assert len([v for v in govern.summary(demo_conn)["violations"] if v["kind"] == "budget_coverage"]) == 4


def test_region_policy_flags_usage_outside_the_list(cur_only, tmp_path):
    policies = {"schemaVersion": "policy-subset-v1", "policies": [
        {"id": "r", "name": "Only US", "kind": "allowed_regions", "params": {"regions": ["us-east-1"]}}]}
    load_governance(cur_only, write(tmp_path, fixtures.organization(), policies))
    s = govern.summary(cur_only)
    assert {v["account"] for v in s["violations"]} == {"prod-commerce", "staging"}
    assert "global" not in by_title(s["violations"], "prod-commerce")["detail"]


def test_disabled_policy_is_not_checked_and_has_no_draft(demo_conn):
    govern.set_policy_enabled(demo_conn, "no-gpu-sandbox", False)
    s = govern.summary(demo_conn)
    assert not [v for v in s["violations"] if v["policy_id"] == "no-gpu-sandbox"]
    assert not [g for g in s["guardrails"] if g["policy_id"] == "no-gpu-sandbox"]
    assert next(p for p in s["policies"] if p["id"] == "no-gpu-sandbox")["result"] == "off"


def test_unknown_policy_cannot_be_toggled(demo_conn):
    with pytest.raises(ValueError):
        govern.set_policy_enabled(demo_conn, "nope", False)


def test_optimize_findings_are_linked(demo_conn):
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    assert v["resources"][0]["optimize"][0]["title"] == "Stop: i-0f0a0001g5"


# ------------------------------------------------------------------ missing data

def test_no_policies_means_no_data(cur_only):
    s = govern.summary(cur_only)
    assert s["state"] == "no_data" and s["violations"] == [] and s["totals"]["spend_nanos"] == 0


def test_no_cur_means_nothing_is_guessed(fresh_conn, fixture_root):
    load_governance(fresh_conn, fixture_root / fixtures.GOV_FOLDER)
    s = govern.summary(fresh_conn)
    assert s["state"] == "no_data" and "CUR" in s["reason"]
    r = Orchestrator(fresh_conn).ask("Show policy violations")
    assert r["kind"] == "govern" and "$" not in r["message"]


def test_no_organization_means_no_draft(cur_only, tmp_path):
    load_governance(cur_only, write(tmp_path, policies=fixtures.policies()))
    s = govern.summary(cur_only)
    assert not s["organization_loaded"]
    for g in s["guardrails"]:
        assert g["document"] is None and g["document_text"] == "" and g["blockers"] and g["next_statuses"] == []
    assert s["violations"]  # policy checks still work without it


def test_account_missing_from_organization_blocks_the_draft(cur_only, tmp_path):
    org = fixtures.organization()
    org["organizationalUnits"] = [ou for ou in org["organizationalUnits"] if ou["id"] != "ou-sandbox"]
    org["serviceControlPolicies"][1]["targets"] = ["ou-prod"]
    load_governance(cur_only, write(tmp_path, org, fixtures.policies()))
    g = gpu_draft(cur_only)
    assert g["document"] is None and "not in the organization data" in g["blockers"][0]
    with pytest.raises(ValueError, match="blockers"):
        govern.set_guardrail_status(cur_only, g["id"], "in_review")


def test_unsupported_charges_give_no_spend_number(demo_conn):
    demo_conn.execute("UPDATE line_items SET amortized_nanos=NULL WHERE resource_id='i-0f0a0001g5' AND usage_date='2026-11-01'")
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    assert v["spend_nanos"] is None and v["spend_display"] == "Not enough evidence"
    assert "cannot be amortized" in v["limitations"][0]


# ------------------------------------------------------------------ drafts

def test_gpu_draft_is_a_valid_scp_for_the_sandbox_account(demo_conn):
    g = gpu_draft(demo_conn)
    statement = g["document"]["Statement"][0]
    assert g["type"] == "SCP" and statement["Effect"] == "Deny" and statement["Action"] == "ec2:RunInstances"
    assert "g5.*" in statement["Condition"]["StringLike"]["ec2:InstanceType"]
    assert [t["id"] for t in g["targets"]] == [SANDBOX]
    assert g["targets"][0]["existing_scps"] == 2  # root + sandbox OU
    assert json.loads(g["document_text"]) == g["document"]
    assert re.fullmatch(r"[A-Za-z0-9]+", statement["Sid"])


def test_region_draft_keeps_global_services_working(demo_conn):
    g = by_title(govern.summary(demo_conn)["guardrails"], "regions")
    statement = g["document"]["Statement"][0]
    assert "iam:*" in statement["NotAction"] and statement["Resource"] == "*"
    assert statement["Condition"]["StringNotEquals"]["aws:RequestedRegion"] == ["us-east-1", "ap-south-1", "eu-west-1"]


def test_tag_policy_strips_the_cur_prefix_and_says_what_it_cannot_do(demo_conn):
    g = by_title(govern.summary(demo_conn)["guardrails"], "team tag")
    assert g["type"] == "Tag policy" and list(g["document"]["tags"]) == ["team"]
    assert any("does not force" in c["detail"] for c in g["checks"])


def test_budget_policy_has_no_guardrail(demo_conn):
    assert not [g for g in govern.summary(demo_conn)["guardrails"] if g["policy_id"] == "budget-every-account"]


def test_every_draft_has_the_required_parts(demo_conn):
    for g in govern.summary(demo_conn)["guardrails"]:
        assert g["checks"] and g["risks"] and g["rollout"] and g["rollback"] and g["limitations"]
        assert "does not apply" in g["execution"]
        assert all(c["state"] != "fail" for c in g["checks"])


def test_oversize_scp_fails_the_check():
    big = {"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Action": "*", "Resource": "*", "Sid": "x" * 6000}]}
    assert check_document(big, "SCP")[0]["state"] == "fail"


@pytest.mark.parametrize("doc", [
    {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]},
    {"Version": "2008-10-17", "Statement": [{"Effect": "Deny", "Action": "*", "Resource": "*"}]},
    {"Version": "2012-10-17", "Statement": []},
    {"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Resource": "*"}]},
])
def test_bad_scp_format_fails_the_check(doc):
    assert check_document(doc, "SCP")[1]["state"] == "fail"


def test_bad_tag_policy_fails_the_check():
    assert check_document({"tags": {}}, "Tag policy")[1]["state"] == "fail"


def test_management_account_target_gets_a_warning(cur_only, tmp_path):
    policies = {"schemaVersion": "policy-subset-v1", "policies": [
        {"id": "m", "name": "No GPU anywhere", "kind": "restricted_instance", "params": {"accounts": [fixtures.PAYER], "families": ["g5"]}}]}
    load_governance(cur_only, write(tmp_path, fixtures.organization(), policies))
    g = govern.summary(cur_only)["guardrails"][0]
    assert g["hits_management"]
    check = next(c for c in g["checks"] if c["check"] == "Management account")
    assert check["state"] == "warn" and "no effect" in check["detail"]


def test_scp_limit_on_a_target_blocks_the_draft(cur_only, tmp_path):
    org = fixtures.organization()
    org["serviceControlPolicies"] += [{"id": f"p-{i}", "name": f"p{i}", "targets": ["ou-sandbox"]} for i in range(3)]
    load_governance(cur_only, write(tmp_path, org, fixtures.policies()))
    assert any("AWS limit" in b for b in gpu_draft(cur_only)["blockers"])


# ------------------------------------------------------------------ impact

def test_impact_says_running_resources_are_not_stopped(demo_conn):
    i = gpu_draft(demo_conn)["impact"]
    assert i["resources"] == 1 and i["spend_display"] == "$724.32" and i["new_in_window"] == 0
    assert "would not be stopped" in i["summary"]


def test_resource_launched_inside_the_window_is_counted_as_new(demo_conn):
    demo_conn.execute("DELETE FROM line_items WHERE resource_id='i-0f0a0001g5' AND usage_date < '2026-10-20'")
    assert gpu_draft(demo_conn)["impact"]["new_in_window"] == 1


def test_zero_impact_is_not_called_proof(demo_conn):
    g = by_title(govern.summary(demo_conn)["guardrails"], "regions")
    assert g["impact"]["resources"] == 0
    assert any("does not prove zero impact" in x for x in g["limitations"])


# ------------------------------------------------------------------ workflow

def test_workflow_happy_path(demo_conn):
    gid = gpu_draft(demo_conn)["id"]
    assert gpu_draft(demo_conn)["status"] == "draft"
    govern.set_guardrail_status(demo_conn, gid, "in_review")
    r = govern.set_guardrail_status(demo_conn, gid, "approved")
    assert r["label"] == "Approved (recorded, not verified)"
    g = gpu_draft(demo_conn)
    assert g["status"] == "approved" and g["next_statuses"] == ["draft"]
    assert govern.summary(demo_conn)["totals"]["approved"] == 1


@pytest.mark.parametrize("first,then", [(None, "approved"), (None, "rejected"), ("in_review", "in_review"), (None, "applied")])
def test_illegal_moves_are_refused(demo_conn, first, then):
    gid = gpu_draft(demo_conn)["id"]
    if first:
        govern.set_guardrail_status(demo_conn, gid, first)
    with pytest.raises(ValueError, match="cannot move"):
        govern.set_guardrail_status(demo_conn, gid, then)


def test_unknown_guardrail_is_refused(demo_conn):
    with pytest.raises(ValueError, match="not found"):
        govern.set_guardrail_status(demo_conn, "0" * 16, "in_review")


def test_approval_is_lost_if_the_draft_changes(demo_conn):
    gid = gpu_draft(demo_conn)["id"]
    govern.set_guardrail_status(demo_conn, gid, "in_review")
    govern.set_guardrail_status(demo_conn, gid, "approved")
    params = {"accounts": [SANDBOX], "families": ["g5", "p5", "trn1"]}
    demo_conn.execute("UPDATE gov_policies SET params_json=? WHERE id='no-gpu-sandbox'", (json.dumps(params),))
    g = gpu_draft(demo_conn)
    assert g["status"] == "draft" and "went back to draft" in g["note"]


# ------------------------------------------------------------------ exceptions

def expiry(conn, days: int) -> str:
    return (engine.reference_date(conn) + timedelta(days=days)).isoformat()


def test_exception_moves_violation_out_of_open_and_spend(demo_conn):
    s = govern.summary(demo_conn)
    v = by_title(s["violations"], "team tag")
    govern.add_exception(demo_conn, v["id"], "research", "GPU trial", expiry(demo_conn, 30))
    s = govern.summary(demo_conn)
    assert by_title(s["violations"], "team tag")["status"] == "excepted"
    assert s["totals"]["open_count"] == 5 and s["totals"]["excepted_count"] == 1
    assert s["totals"]["spend_display"] == "$724.32"  # the GPU still breaks the other policy


def test_expired_exception_opens_the_violation_again(demo_conn):
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    govern.add_exception(demo_conn, v["id"], "research", "GPU trial", expiry(demo_conn, 3))
    assert by_title(govern.summary(demo_conn)["violations"], "GPU")["status"] == "excepted"
    store.set_meta(demo_conn, "reference_date", expiry(demo_conn, 4))
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    assert v["status"] == "open" and "expired" in v["limitations"][0]


def test_exception_is_still_active_on_its_last_day(demo_conn):
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    last = expiry(demo_conn, 3)
    govern.add_exception(demo_conn, v["id"], "research", "GPU trial", last)
    store.set_meta(demo_conn, "reference_date", last)
    assert by_title(govern.summary(demo_conn)["violations"], "GPU")["status"] == "excepted"


@pytest.mark.parametrize("owner,reason,days,phrase", [
    ("", "r", 10, "owner"), ("o", "", 10, "reason"), ("o", "r", 0, "expiry"), ("o", "r", -5, "expiry"), ("o", "r", 366, "expiry"),
])
def test_bad_exceptions_are_refused(demo_conn, owner, reason, days, phrase):
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    with pytest.raises(ValueError, match=phrase):
        govern.add_exception(demo_conn, v["id"], owner, reason, expiry(demo_conn, days))


def test_exception_needs_a_real_date_and_violation(demo_conn):
    v = by_title(govern.summary(demo_conn)["violations"], "GPU")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        govern.add_exception(demo_conn, v["id"], "o", "r", "soon")
    with pytest.raises(ValueError, match="not found"):
        govern.add_exception(demo_conn, "0" * 16, "o", "r", expiry(demo_conn, 5))


# ------------------------------------------------------------------ workers, chat and safety

def test_workers_have_fixed_allow_lists(demo_conn):
    p = govern.GovernancePipeline(demo_conn)
    for w in (p.checks, p.impact, p.risk, p.guardrails):
        with pytest.raises(PermissionError):
            w.call("apply_scp", target="root")


def test_run_is_audited_and_stable(demo_conn):
    a, b = govern.summary(demo_conn), govern.summary(demo_conn)
    assert a["violations"] == b["violations"] and a["guardrails"] == b["guardrails"]
    assert demo_conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='govern.run'").fetchone()[0] == 2


def test_all_modules_are_live():
    assert "govern" in ACTIVE_WORKERS and not PLANNED_WORKERS


@pytest.mark.parametrize("question", [
    "Which spend has no owner?", "Draft a guardrail for GPUs in sandbox", "Show policy violations",
    "block EC2 spend with an SCP", "are we compliant?", "show governance status",
])
def test_governance_questions_route_to_govern(demo_conn, question):
    assert Orchestrator(demo_conn).ask(question)["kind"] == "govern"


@pytest.mark.parametrize("question", ["Untagged spend by account last month", "Show cost by service for last month", "How can I reduce EC2 cost?"])
def test_other_questions_do_not_route_to_govern(demo_conn, question):
    assert Orchestrator(demo_conn).ask(question).get("kind") != "govern"


def test_chat_no_owner_matches_the_page(demo_conn):
    r = Orchestrator(demo_conn).ask("Which spend has no owner?")
    assert "$808.07" in r["message"] and "4 resources" in r["message"]
    assert [v["kind"] for v in r["violations"]] == ["required_tag"]


def test_chat_draft_for_gpus_returns_one_draft(demo_conn):
    r = Orchestrator(demo_conn).ask("Draft a guardrail for GPUs in sandbox")
    assert [g["policy_id"] for g in r["guardrails"]] == ["no-gpu-sandbox"]
    assert "$724.32" in r["message"] and "does not apply" in r["message"]


def test_chat_with_several_drafts_does_not_quote_one_impact(demo_conn):
    r = Orchestrator(demo_conn).ask("block EC2 spend with an SCP")
    assert len(r["guardrails"]) == 3 and "Each draft shows its own impact" in r["message"]
    assert "AmazonEC2" not in r["message"]  # the service does not narrow drafts, so it is not named


def test_chat_scope_by_account(demo_conn):
    r = Orchestrator(demo_conn).ask("Show policy violations for staging")
    assert {v["account"] for v in r["violations"]} == {"staging"}


def test_data_sources_show_the_organization(demo_conn):
    states = {s["key"]: s["state"] for s in data_status(demo_conn)["sources"]}
    assert states["organizations"] == "connected"


def test_govern_code_has_no_network_or_aws_client():
    root = Path(__file__).parent.parent / "costlens"
    for path in (root / "agent" / "govern.py", root / "ingest" / "governance.py"):
        text = path.read_text(encoding="utf-8")
        for word in ("import boto", "urllib", "http.client", "socket", "subprocess", "requests"):
            assert word not in text, f"{word} in {path.name}"
