import io
import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from costlens import cloud_app
from costlens.agent import cloud_runtime, session
from costlens.agent.orchestrator import Orchestrator
from costlens.query.cloud_dashboard import dashboard, dimensions, status
from costlens.query.model import CostQuery
from costlens.server import serve
from tests.test_server import call


class Backend:
    def __init__(self):
        self.scope=SimpleNamespace(synthetic=True,accounts=("210987654321",),reference_date="2026-11-03",tag_keys=("user:team",),
                                   database="pilot",tables={"cur":"cur"},max_rows=500)
    def _aggregate(self,q):
        rows=[{"bucket":"Total" if q.granularity=="total" else "2026-10-01","key":"AmazonEC2","nanos":1_000_000_000,"days":["2026-10-01"]},
              {"bucket":"Total" if q.granularity=="total" else "2026-10-02","key":"AmazonS3","nanos":2_000_000_000,"days":["2026-10-02"]}]
        return rows,{"query_execution_id":"query-123"}
    def _dimension(self,name):
        return "service"
    def _execute(self,*args):
        return [{"dimension":"service","value":"AmazonEC2"},{"dimension":"account","value":"210987654321"},{"dimension":"tag:user:team","value":"checkout"}],{}
    def coverage(self,*args):
        return {"periods":[{"period":"2026-10","first_day":"2026-10-01","data_through":"2026-10-31","rows":"123"}],
                "evidence":[{"query_execution_id":"coverage-123"}]}


QUERY={"start":"2026-10-01","end":"2026-11-01","granularity":"total","group_by":"service"}


def test_dashboard_contract_preserves_total_and_other():
    result=dashboard(Backend(),{**QUERY,"top_n":1})
    assert result["total_display"]=="$3.00"
    assert {row["key"]:row["total_display"] for row in result["series"]}=={"AmazonS3":"$2.00","Other":"$1.00"}
    assert sum(result["bucket_totals_cents"])==300
    assert result["days_with_data"]==2
    assert result["daily_average_display"]=="$1.50"
    assert result["coverage"]["periods"][0]["status"]=="observed"
    assert result["calculation"]["sources"][0]["execution_id"]=="query-123"


def test_daily_chart_does_not_fill_unobserved_days():
    result=dashboard(Backend(),{**QUERY,"granularity":"daily"})
    assert result["buckets"]==["2026-10-01","2026-10-02"]
    assert len(result["series"][0]["values_cents"])==2


def test_variance_contract_and_no_invented_resource_drivers():
    result=dashboard(Backend(),{**QUERY,"kind":"variance","compare_start":"2026-09-01","compare_end":"2026-10-01"})
    assert result["baseline"]["total_display"]==result["current"]["total_display"]=="$3.00"
    assert result["drivers"]=={"groups":[],"increases":[],"decreases":[]}


def test_cloud_dimensions_and_status_use_queried_scope():
    values=dimensions(Backend())
    assert values["tags"]["user:team"]==["checkout"]
    result=status(Backend())
    assert result["backend"]=="cloud" and result["synthetic"] is True
    assert result["deliveries"][0]["row_count"]==123


def test_runtime_dashboard_operation_does_not_call_a_model(monkeypatch):
    class Tools:
        def initialize(self): pass
        def call(self,name,**arguments):
            assert name=="dashboard_query"
            return {"status":"ok","total_display":"$3.00"}
    monkeypatch.setattr(cloud_runtime,"make_client",lambda:pytest.fail("model must not run for charts"))
    assert cloud_runtime.invoke({"operation":"query","query":QUERY},tools=Tools())["result"]["total_display"]=="$3.00"


def test_runtime_rejects_oversized_or_untrusted_context():
    with pytest.raises(ValueError):
        cloud_runtime.invoke({"question":"x","workspace_id":"another"})


def test_client_is_iam_scoped_and_does_not_retry_paid_requests(monkeypatch):
    monkeypatch.setenv("COSTLENS_AGENTCORE_RUNTIME_ARN","arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/pilot-123")
    calls=[]
    class SDK:
        def invoke_agent_runtime(self,**kwargs):
            calls.append(kwargs)
            return {"response":io.BytesIO(json.dumps({"status":"ok","result":{"status":"ok"}}).encode())}
    cloud_app.Client(SDK()).invoke("query",query=QUERY)
    assert json.loads(calls[0]["payload"])["operation"]=="query"
    assert "credentials" not in json.loads(calls[0]["payload"])
    monkeypatch.setenv("COSTLENS_AGENTCORE_RUNTIME_ARN","https://untrusted.example")
    with pytest.raises(cloud_app.CloudUnavailable): cloud_app.Client(SDK())


@pytest.fixture
def cloud_client(monkeypatch,demo_conn):
    monkeypatch.setenv("COSTLENS_BACKEND","cloud")
    requests=[]
    from costlens.agent.optimize import summary
    report=summary(demo_conn)
    report.update(workflow_read_only=True,backend="cloud",source_snapshot_as_of="2026-11-03T00:00:00Z")
    class Fake:
        def invoke(self,operation,**arguments):
            requests.append((operation,arguments))
            if operation=="ask":
                return {"status":"ok","kind":"investigation","message":"Synthetic cost is $3.00.","trace":[],"evidence_queries":[{"title":"Evidence","query":QUERY}],
                        "input_tokens":10,"output_tokens":5,"suggestions":[],"mode":cloud_app.mode()}
            if operation=="query": return dashboard(Backend(),arguments["query"])
            if operation=="status": return {"status":"ok","data_status":status(Backend())}
            if operation=="dimensions": return {"status":"ok","dimensions":dimensions(Backend())}
            if operation=="opportunities": return {"status":"ok","opportunities":[]}
            if operation=="optimization":
                return {"status":"ok","summary":json.loads(json.dumps(report))}
    monkeypatch.setattr(cloud_app,"Client",Fake)
    return requests


def test_cloud_chat_saves_context_and_run_in_app_db(fresh_conn,cloud_client):
    session.add_memory(fresh_conn,"local","Focus on checkout")
    response=Orchestrator(fresh_conn).ask("October cost?")
    assert response["backend"]=="cloud" and response["run_id"]
    assert cloud_client[0][1]["notes"]==["Focus on checkout"]
    report=session.save_report(fresh_conn,"local",response["run_id"])
    assert session.get_report(fresh_conn,"local",report)["current"][0]["summary"].endswith("$3.00.")
    assert cloud_client[-1][0]=="query"


def test_cloud_failures_never_use_local_fixtures(fresh_conn,cloud_client,monkeypatch):
    class Fail:
        def invoke(self,*args,**kwargs): raise cloud_app.CloudUnavailable("Cloud offline")
    monkeypatch.setattr(cloud_app,"Client",Fail)
    with pytest.raises(cloud_app.CloudUnavailable): Orchestrator(fresh_conn).ask("Show cost by service last month")


def test_http_cloud_queries_and_disabled_local_workflows(fresh_conn,tmp_path,cloud_client):
    httpd=serve(str(tmp_path/"state.sqlite"),"127.0.0.1",0)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    port=httpd.server_address[1]
    try:
        assert call(port,"GET","/api/status")[1]["backend"]=="cloud"
        assert call(port,"GET","/api/dimensions")[1]["service"]
        assert call(port,"POST","/api/query",{"query":QUERY})[1]["result"]["total_display"]=="$3.00"
        assert call(port,"GET","/api/opportunities")[0]==200
        assert call(port,"POST","/api/budgets",{"spec":{}})[0]==501
        assert call(port,"DELETE","/api/optimize/123/implemented",{})[0]==501
    finally:
        httpd.shutdown();httpd.server_close()


def test_cloud_http_failure_is_503(tmp_path,cloud_client,monkeypatch):
    monkeypatch.setattr(cloud_app,"query",lambda _:(_ for _ in ()).throw(cloud_app.CloudUnavailable("Cloud offline")))
    httpd=serve(str(tmp_path/"state.sqlite"),"127.0.0.1",0)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    try:
        assert call(httpd.server_address[1],"POST","/api/query",{"query":QUERY})[0]==503
    finally: httpd.shutdown();httpd.server_close()
