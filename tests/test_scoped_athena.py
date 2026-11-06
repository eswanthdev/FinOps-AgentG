import copy
from types import SimpleNamespace

import pytest

from costlens.query.athena import AthenaBackend, AthenaScope
from costlens.agent.cloud_tools import dispatch
from costlens.agent.mcp_client import GatewayTools
from costlens.agent.cloud_runtime import invoke
from costlens.agent.investigator import ModelTurn


def scope_dict():
    sources = ("cur", "cost-optimization-hub", "compute-optimizer", "trusted-advisor", "savings-plans")
    return dict(region="eu-central-1", account_id="123456789012", workspace_id="synthetic-pilot",
                database="costlens_pilot", workgroup="costlens-scoped", results_uri="s3://query-results-test/queries/",
                accounts=["210987654321"], tables={s: s.replace("-", "_") for s in sources},
                locations={s: f"s3://source-{s}-test/runs/v1/" for s in sources})


class Glue:
    def __init__(self, scope):
        self.scope = scope
        self.location_override = None
        self.partition_override = None

    def get_table(self, **kwargs):
        source = next(s for s, table in self.scope.tables.items() if table == kwargs["Name"])
        return {"Table": {"TableType": "EXTERNAL_TABLE", "StorageDescriptor": {
            "Location": self.location_override or self.scope.locations[source]}}}

    def get_partitions(self, **kwargs):
        return {"Partitions": ([{"StorageDescriptor": {"Location": self.partition_override}}]
                               if self.partition_override else [])}


class Athena:
    def __init__(self, scope, rows=None):
        self.scope, self.requests, self.stopped = scope, [], []
        self.rows = rows if rows is not None else [["Total", "AmazonEC2", "12340000000", "0"]]
        self.config = {"EnforceWorkGroupConfiguration": True, "BytesScannedCutoffPerQuery": 10_000_000,
                       "ResultConfiguration": {"OutputLocation": scope.results_uri, "ExpectedBucketOwner": scope.account_id,
                                               "EncryptionConfiguration": {"EncryptionOption": "SSE_S3"}}}
        self.state = "SUCCEEDED"
        self.scan = 1024

    def get_work_group(self, **kwargs):
        assert kwargs["WorkGroup"] == self.scope.workgroup
        return {"WorkGroup": {"Configuration": self.config}}

    def start_query_execution(self, **kwargs):
        self.requests.append(kwargs)
        return {"QueryExecutionId": "test-query"}

    def get_query_execution(self, **kwargs):
        return {"QueryExecution": {"Status": {"State": self.state}, "Statistics": {"DataScannedInBytes": self.scan}}}

    def stop_query_execution(self, **kwargs):
        self.stopped.append(kwargs)

    def get_query_results(self, **kwargs):
        columns = ["bucket", "group_key", "nanos", "invalid_rows"]
        return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": [{"Name": c} for c in columns]},
                              "Rows": [{"Data": [{"VarCharValue": v} for v in row]} for row in [columns, *self.rows]]}}


@pytest.fixture
def backend():
    scope = AthenaScope.from_dict(scope_dict())
    return AthenaBackend(scope, Athena(scope), Glue(scope), sleep=lambda _: None)


QUERY = {"start": "2026-10-01", "end": "2026-11-01", "granularity": "total", "group_by": "service"}


def test_exact_aggregate_and_fixed_scope(backend):
    result = backend.query(QUERY)
    assert result["total_display"] == "$12.34"
    request = backend.athena.requests[0]
    assert request["WorkGroup"] == backend.scope.workgroup
    assert request["ExecutionParameters"][-1] == "'210987654321'"
    assert '"costlens_pilot"."cur"' in request["QueryString"]
    assert "truncate" in request["QueryString"]  # half-even, not simple half-up


def test_filter_injection_remains_a_parameter(backend):
    hostile = "EC2'); DROP TABLE cur; --"
    assert backend.query({**QUERY, "filters": {"service": [hostile]}})["status"] == "ok"
    request = backend.athena.requests[0]
    assert hostile not in request["QueryString"]
    assert request["ExecutionParameters"][-1] == "'EC2''); DROP TABLE cur; --'"


def test_account_escape_and_raw_sql_rejected(backend):
    with pytest.raises(PermissionError):
        backend.query({**QUERY, "filters": {"account": ["999999999999"]}})
    with pytest.raises(ValueError):
        backend.query({**QUERY, "sql": "SELECT * FROM other"})
    assert not backend.athena.requests


@pytest.mark.parametrize("location,partition", [("s3://other/data/", None), (None, "s3://other/data/")])
def test_catalog_and_partition_escape_rejected(backend, location, partition):
    backend.glue.location_override, backend.glue.partition_override = location, partition
    with pytest.raises(PermissionError):
        backend.query(QUERY)
    assert not backend.athena.requests


def test_scan_limit_cancels_query(backend):
    backend.athena.scan = 10_000_001
    with pytest.raises(TimeoutError):
        backend.query(QUERY)
    assert backend.athena.stopped


def test_timeout_cancels_query(backend):
    backend.athena.state = "RUNNING"
    ticks = iter([0, 46])
    backend.clock = lambda: next(ticks)
    with pytest.raises(TimeoutError):
        backend.query(QUERY)
    assert backend.athena.stopped


def test_bad_currency_and_empty_data_do_not_invent_total(backend):
    backend.athena.rows[0][-1] = "1"
    assert backend.query(QUERY)["status"] == "insufficient_data"
    backend.athena.rows = []
    result = backend.query(QUERY)
    assert result["status"] == "insufficient_data" and "total_display" not in result


def test_result_limit_is_not_silent_truncation(backend):
    backend.athena.rows *= 501
    assert backend.query(QUERY)["status"] == "insufficient_data"


def test_workgroup_must_enforce_limits(backend):
    backend.athena.config["EnforceWorkGroupConfiguration"] = False
    with pytest.raises(PermissionError):
        backend.query(QUERY)


def test_top_n_preserves_other_total(backend):
    backend.athena.rows = [["Total", "EC2", "1000000000", "0"], ["Total", "S3", "2000000000", "0"]]
    result = backend.query({**QUERY, "top_n": 1})
    assert result["total_display"] == "$3.00"
    assert {r["group"]: r["cost_display"] for r in result["rows"]} == {"S3": "$2.00", "Other": "$1.00"}


def test_scope_cannot_use_source_for_results_or_inject_identifier():
    config = scope_dict()
    config["results_uri"] = config["locations"]["cur"]
    with pytest.raises(ValueError):
        AthenaScope.from_dict(config)
    config = scope_dict()
    config["tables"]["cur"] = "cur;drop"
    with pytest.raises(ValueError):
        AthenaScope.from_dict(config)


def test_gateway_dispatch_and_storage_are_scoped(backend):
    with pytest.raises(PermissionError):
        dispatch(backend, "delete_resource", {})
    with pytest.raises(ValueError):
        dispatch(backend, "storage_metrics", {"account": "", "bucket": "", "role": "admin"})
    result = dispatch(backend, "storage_metrics", {"account": "", "bucket": ""}, [
        {"account_id": "210987654321", "bucket": "synthetic-bucket", "value": 42},
        {"account_id": "999999999999", "bucket": "other", "value": 99}])
    assert len(result["metrics"]) == 1 and result["synthetic_data"] is True


def test_mcp_call_is_fixed_and_unwraps_tool_json():
    requests = []
    def transport(payload):
        requests.append(payload)
        return {"id": payload["id"], "result": {"content": [{"type": "text", "text": '{"status":"ok"}'}]}}
    tools = GatewayTools("https://test.gateway.bedrock-agentcore.eu-central-1.amazonaws.com/mcp", "eu-central-1", transport=transport)
    assert tools.call("storage_metrics", account="", bucket="")["status"] == "ok"
    assert requests[0]["params"]["name"] == "finops___storage_metrics"
    with pytest.raises(PermissionError):
        tools.call("arbitrary_aws_call")
    with pytest.raises(ValueError):
        GatewayTools("https://evil.example/mcp", "eu-central-1")


def test_runtime_reuses_number_checks_and_fixed_tools(monkeypatch):
    monkeypatch.setenv("COSTLENS_REFERENCE_DATE", "2026-11-03")
    monkeypatch.setenv("COSTLENS_SYNTHETIC", "true")
    class Tools:
        def initialize(self):
            return {}
        def call(self, tool, **arguments):
            assert tool == "query_costs"
            return {"status": "ok", "total_display": "$12.34"}
    turns = iter([ModelTurn("tool_use", [{"type": "tool_use", "id": "t", "name": "query_costs", "input": {}}]),
                  ModelTurn("end_turn", [{"type": "text", "text": "Synthetic cost is $12.34."}])])
    class Client:
        def create(self, system, messages, tools):
            assert {t["name"] for t in tools} == {"query_costs", "data_coverage", "list_opportunities", "list_recommendations", "storage_metrics"}
            return next(turns)
    result = invoke({"question": "Cost?"}, tools=Tools(), client=Client())
    assert result["status"] == "ok" and result["synthetic_data"] is True
    with pytest.raises(ValueError):
        invoke({"question": "x", "workspace": "other"}, tools=Tools(), client=Client())


def test_cloud_schema_is_explicit_about_month_end_and_grouping():
    from costlens.agent.cloud_tools import SPECS, gateway_schema
    props = SPECS[0]["input_schema"]["properties"]
    assert "EXCLUSIVE" in props["end"]["description"]
    assert "empty" not in props["group_by"]["enum"]
    assert "service" in props["group_by"]["enum"]
    assert "enum" not in gateway_schema(props["group_by"])


def test_rejected_tool_result_is_marked_for_model_repair(fresh_conn):
    from costlens.agent.investigator import investigate
    from datetime import date
    class Tools:
        def call(self, tool, **arguments):
            return {"status":"rejected","reason":"unsupported group_by"}
    class Client:
        count=0
        def create(self, system, messages, tools):
            self.count+=1
            if self.count==1:
                return ModelTurn("tool_use",[{"type":"tool_use","id":"t","name":"query_costs","input":{}}])
            assert messages[-1]["content"][0]["is_error"] is True
            return ModelTurn("end_turn",[{"type":"text","text":"The query input was rejected."}])
    result=investigate(fresh_conn,Client(),"x",tool_backend=Tools(),reference_date=date(2026,11,3))
    assert result["trace"][0]["status"] == "rejected"
