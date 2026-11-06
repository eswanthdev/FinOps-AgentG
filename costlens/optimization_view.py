"""Service-scoped presentation of an already validated optimization report."""
import copy

from .agent.optimize import _totals
from .agent.verify import _totals as verification_totals
from .query.model import QueryError

LABELS={"AmazonEC2":"EC2","AmazonEBS":"EBS","AmazonRDS":"RDS","AmazonS3":"S3",
        "ComputeSavingsPlans":"Savings Plans","AmazonDynamoDB":"DynamoDB","AWSLambda":"Lambda"}


def service_key(finding):
    return "AmazonEBS" if finding.get("resource_type")=="EbsVolume" else finding["service"]


def for_service(report, service="all"):
    result=copy.deepcopy(report)
    if report.get("state")!="ok":
        return result
    counts={key:0 for key in ("AmazonEC2","AmazonEBS","AmazonRDS","AmazonS3")}
    for finding in report["opportunities"]:
        key=service_key(finding)
        counts.setdefault(key,0)
        if finding["status"]!="dismissed": counts[key]+=1
    if service!="all" and service not in counts:
        raise QueryError("unsupported optimization service")
    result["available_services"]=[{"key":"all","label":"All services","count":sum(counts.values())}]+[
        {"key":key,"label":LABELS.get(key,key),"count":value} for key,value in counts.items()]
    result["service_filter"]=service
    result["service_label"]="All services" if service=="all" else LABELS.get(service,service)
    if service=="all": return result
    result["opportunities"]=[finding for finding in result["opportunities"] if service_key(finding)==service]
    result["totals"]=_totals(result["opportunities"])
    ids={finding["id"] for finding in result["opportunities"]}
    changes=[change for change in result["verification"]["results"] if change["finding_id"] in ids]
    result["verification"]["results"]=changes
    measured=verification_totals(changes)
    result["verification"]["totals"]=measured
    if changes:
        result["totals"]["realised_display"]=measured["final_display"]
        result["totals"]["realised_sub"]=f"final billing, {measured['provisional_display']} provisional"
    result["data_quality"]=report["data_quality"]+[{
        "check":"Selected service","state":"ok","detail":f"Showing {result['service_label']} recommendations. Data-source checks above describe the full snapshot."}]
    return result
