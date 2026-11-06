"""Fixed tools exposed through the authenticated AgentCore Gateway."""
from __future__ import annotations

import json
import os
import copy
from pathlib import Path

from ..query.athena import AthenaBackend, AthenaScope
from .investigator import TOOL_SPECS

SPECS = copy.deepcopy([spec for spec in TOOL_SPECS if spec["name"] in ("query_costs", "data_coverage")])
_query_properties = SPECS[0]["input_schema"]["properties"]
_query_properties["start"] = {"type":"string","description":"Inclusive start date, YYYY-MM-DD."}
_query_properties["end"] = {"type":"string","description":"EXCLUSIVE end date, YYYY-MM-DD. For a full calendar month use the first day of the NEXT month."}
_query_properties["group_by"] = {"type":"string","enum":["service","account","region","usage_type","line_item_type","resource",
    "tag:user:team","tag:user:env","tag:user:app",""],"description":"For total bills use service. For no grouping use the zero-length string, never the word empty."}
SPECS += [
    {"name": "list_opportunities", "description": "Use first for savings questions: common, deduplicated opportunities with source links and alternatives. Claims remain unvalidated and are not additive.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "account": {"type": "string", "description": "Allowed account ID, or empty for the configured workspace"}}, "required": ["account"]}},
    {"name": "list_recommendations", "description": "Source recommendation evidence. Sources overlap; never sum their estimates or call them realised savings.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "source": {"type": "string", "enum": ["cost-optimization-hub", "compute-optimizer", "trusted-advisor", "savings-plans"]},
         "account": {"type": "string", "description": "Allowed account ID, or empty for the configured workspace"}}, "required": ["source", "account"]}},
    {"name": "storage_metrics", "description": "Storage evidence through MCP. In the synthetic pilot these are invented fixtures; missing production telemetry is reported explicitly.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "account": {"type": "string"}, "bucket": {"type": "string"}}, "required": ["account", "bucket"]}},
]
MODEL_SPECS = list(SPECS)
SPECS += [
    {"name":"dashboard_optimization","description":"Validated optimization report built from the registered export snapshot.","input_schema":{"type":"object","additionalProperties":False,"properties":{},"required":[]}},
    {"name":"dashboard_query","description":"Render a validated dashboard query for the application.","input_schema":{
        "type":"object","additionalProperties":False,"properties":{"query":{"type":"object","description":"CostQuery JSON object, validated by the service"}},"required":["query"]}},
    {"name":"dashboard_dimensions","description":"Scoped dimension values for application filters.","input_schema":{"type":"object","additionalProperties":False,"properties":{},"required":[]}},
    {"name":"dashboard_status","description":"Live source coverage for the application.","input_schema":{"type":"object","additionalProperties":False,"properties":{},"required":[]}},
]
NAMES = frozenset(spec["name"] for spec in SPECS)


def gateway_schema(schema):
    """Gateway's API supports a subset of JSON Schema; handler keeps full validation."""
    value = {key: schema[key] for key in ("type", "required", "description") if key in schema}
    if "enum" in schema:
        value["description"] = (value.get("description", "") + " Allowed: " + ", ".join(schema["enum"])).strip()
    if "properties" in schema:
        value["properties"] = {key: gateway_schema(item) for key, item in schema["properties"].items()}
    if "items" in schema:
        value["items"] = gateway_schema(schema["items"])
    return value


def dispatch(backend: AthenaBackend, name: str, arguments: dict, storage_fixture: list[dict] | None = None):
    if name not in NAMES or not isinstance(arguments, dict):
        raise PermissionError("tool is outside the fixed allow-list")
    spec = next(spec for spec in SPECS if spec["name"] == name)["input_schema"]
    if set(arguments) - set(spec["properties"]) or not set(spec["required"]) <= set(arguments):
        raise ValueError("tool fields do not match its schema")
    if name.startswith("dashboard_"):
        from ..query import cloud_dashboard
        if name=="dashboard_optimization":
            report_path=Path(__file__).resolve().parents[2]/"optimization_summary.json"
            if not report_path.exists():
                return {"status":"unavailable","reason":"Optimization analysis is not published yet"}
            report=json.loads(report_path.read_text())
            if not backend.scope.synthetic or report.get("synthetic_data") is not True:
                return {"status":"unavailable","reason":"This report is for the synthetic pilot only"}
            allowed=set(backend.scope.accounts)
            if any(f["account_id"] not in allowed for f in report.get("opportunities",[])):
                raise PermissionError("report account scope differs from this workspace")
            for origin in report.get("source_provenance",[]):
                if not origin.get("raw_location") or not backend.scope.locations.get(origin["source"],"").startswith(origin["raw_location"]):
                    raise PermissionError("report source locations differ from this workspace")
            return {"status":"ok","summary":report}
        if name == "dashboard_query":
            return cloud_dashboard.dashboard(backend,arguments["query"])
        if name == "dashboard_dimensions":
            return {"status":"ok","dimensions":cloud_dashboard.dimensions(backend)}
        return {"status":"ok","data_status":cloud_dashboard.status(backend)}
    if name == "query_costs":
        raw = dict(arguments)
        filters = raw.pop("filters")
        if not isinstance(filters, list) or len(filters) > 8:
            raise ValueError("filters must be a bounded list")
        mapped = {}
        for entry in filters:
            if not isinstance(entry, dict) or set(entry) != {"dimension", "values"} or entry["dimension"] in mapped:
                raise ValueError("invalid or duplicate filter dimension")
            mapped[entry["dimension"]] = entry["values"]
        raw["filters"] = mapped
        return backend.query(raw)
    if name == "data_coverage":
        return backend.coverage(**arguments)
    if name == "list_recommendations":
        return backend.recommendations(**arguments)
    if name == "list_opportunities":
        return backend.opportunities(**arguments)
    if not all(isinstance(arguments[k], str) for k in ("account", "bucket")):
        raise ValueError("storage selectors must be strings")
    if arguments["account"] and arguments["account"] not in backend.scope.accounts:
        raise PermissionError("requested account is outside this workspace")
    if not backend.scope.synthetic:
        return {"status": "unavailable", "reason": "Production storage telemetry connector is not configured; configuration APIs are not usage metrics."}
    rows = [row for row in storage_fixture or [] if row["account_id"] in backend.scope.accounts
            and (not arguments["account"] or row["account_id"] == arguments["account"])
            and (not arguments["bucket"] or row["bucket"] == arguments["bucket"])]
    return {"status": "ok" if rows else "insufficient_data", "synthetic_data": True, "metrics": rows,
            "note": "Invented storage metrics, not live AWS readings or a dollar savings estimate."}


def lambda_handler(event, context):
    """Gateway supplies the tool identity in trusted Lambda client context."""
    custom = getattr(getattr(context, "client_context", None), "custom", {}) or {}
    name = custom.get("bedrockAgentCoreToolName", "")
    prefix = os.environ.get("COSTLENS_GATEWAY_TARGET", "finops") + "___"
    if not name.startswith(prefix):
        raise PermissionError("request did not identify a registered Gateway tool")
    import boto3
    from botocore.config import Config
    scope = AthenaScope.from_dict(json.loads(os.environ["COSTLENS_ATHENA_SCOPE"]))
    sdk_config = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2})
    backend = AthenaBackend(scope, boto3.client("athena", region_name=scope.region, config=sdk_config),
                            boto3.client("glue", region_name=scope.region, config=sdk_config))
    fixture = json.loads(os.environ.get("COSTLENS_STORAGE_FIXTURE", "[]"))
    fixture_path = os.environ.get("COSTLENS_STORAGE_FIXTURE_PATH")
    if scope.synthetic and fixture_path:
        fixture = json.loads(Path(fixture_path).read_text())
    try:
        return dispatch(backend, name[len(prefix):], event, fixture)
    except ValueError as exc:
        return {"status": "rejected", "reason": "Invalid typed input: " + str(exc)[:300]}
    except (TypeError, KeyError, PermissionError, TimeoutError):
        return {"status": "rejected", "reason": "Tool input, configured scope or execution bounds were not satisfied."}
