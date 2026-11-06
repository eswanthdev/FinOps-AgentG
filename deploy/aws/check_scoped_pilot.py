"""Live pilot checks. --model explicitly opts into one paid Bedrock investigation."""
import argparse
import json
import uuid
from pathlib import Path

import boto3

from costlens.agent.mcp_client import GatewayTools
from costlens.query.athena import AthenaBackend, AthenaScope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="costlens-demo")
    parser.add_argument("--model", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    scope = AthenaScope.from_dict(json.loads((root/"var/scoped-athena.json").read_text()))
    state = json.loads((root/"var/scoped-pilot-deployment.json").read_text())
    session = boto3.Session(profile_name=args.profile,region_name=scope.region)
    backend = AthenaBackend(scope,session.client("athena"),session.client("glue"))
    query = {"start":"2026-10-01","end":"2026-11-01","group_by":"service","granularity":"total"}
    result = backend.query(query)
    if result.get("total_display") != "$18,561.93" or result.get("total_nanos") != "18561934352639":
        raise AssertionError(result)
    print("Athena billing total verified: " + result["total_display"],flush=True)
    mcp = GatewayTools(state["gateway_url"],scope.region,session=session)
    mcp.initialize()
    tools = mcp.discover()
    print("Authenticated MCP tools: " + ", ".join(tools),flush=True)
    tool_query = {"kind":"explore","start":"2026-10-01","end":"2026-11-01","metric":"unblended",
                  "granularity":"total","group_by":"service","filters":[],"top_n":8,"compare_start":"","compare_end":""}
    remote = mcp.call("query_costs",**tool_query)
    if remote.get("total_display") != result["total_display"]:
        raise AssertionError(remote)
    print("MCP -> scoped Lambda -> Athena total verified",flush=True)
    common = mcp.call("list_opportunities",account="")
    if common.get("status") != "ok" or not common.get("opportunities"):
        raise AssertionError(common)
    assert all(row["counted"] == "false" for row in common["opportunities"])
    print(f"Common deduplicated opportunities verified: {len(common['opportunities'])}",flush=True)
    contexts = {}
    for source in ("cost-optimization-hub","compute-optimizer","trusted-advisor","savings-plans"):
        recommendations = mcp.call("list_recommendations",source=source,account="")
        if recommendations.get("status") != "ok" or not recommendations.get("recommendations"):
            raise AssertionError(recommendations)
        contexts[source] = len(recommendations["recommendations"])
        print(f"MCP source verified: {source} ({contexts[source]} rows)",flush=True)
    storage = mcp.call("storage_metrics",account="",bucket="")
    if storage.get("synthetic_data") is not True or not storage.get("metrics"):
        raise AssertionError(storage)
    runtime = session.client("bedrock-agentcore")
    def invoke(payload):
        response = runtime.invoke_agent_runtime(agentRuntimeArn=state["runtime_arn"],runtimeSessionId=str(uuid.uuid4()),
            contentType="application/json",accept="application/json",payload=json.dumps(payload).encode())
        body = response["response"].read()
        value = json.loads(body)
        if value.get("status") != "ok":
            raise AssertionError(value)
        return value
    health = invoke({"operation":"health"})
    print("AgentCore runtime -> authenticated Gateway discovery verified",flush=True)
    report = {"athena":result,"mcp_total":remote["total_display"],"source_row_counts":contexts,
              "common_opportunities":len(common["opportunities"]),"storage_fixture_rows":len(storage["metrics"]),"runtime_health":health}
    if args.model:
        answer = invoke({"question":"What was our total unblended AWS cost in October 2026? Use query_costs for 2026-10-01 to 2026-11-01, total granularity, and state that it is synthetic."})
        if "$18,561.93" not in answer.get("message", ""):
            raise AssertionError(answer)
        report["model_answer"] = answer
        print("Checked model answer: " + answer["message"],flush=True)
    (root/"var/scoped-pilot-verification.json").write_text(json.dumps(report,indent=2)+"\n")


if __name__ == "__main__":
    main()
