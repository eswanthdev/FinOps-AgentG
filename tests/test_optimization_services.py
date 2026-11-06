import pytest

from costlens.agent.optimize import summary
from costlens.optimization_view import for_service,service_key
from costlens.query.model import QueryError


def test_service_filters_findings_and_exact_totals(demo_conn):
    report=summary(demo_conn)
    all_services=for_service(report)
    assert all_services["totals"]["counted_display"]=="$3,272.06"
    totals=0
    for service in all_services["available_services"][1:]:
        filtered=for_service(report,service["key"])
        assert all(service_key(f)==service["key"] for f in filtered["opportunities"])
        assert len(filtered["opportunities"])==service["count"]
        totals+=filtered["totals"]["counted_nanos"]
    assert totals==report["totals"]["counted_nanos"]
    assert len(report["opportunities"])==26


def test_ebs_is_separate_from_ec2_and_s3_is_honestly_empty(demo_conn):
    report=summary(demo_conn)
    ebs=for_service(report,"AmazonEBS")
    assert ebs["opportunities"] and all(f["resource_type"]=="EbsVolume" for f in ebs["opportunities"])
    assert not any(f["resource_type"]=="EbsVolume" for f in for_service(report,"AmazonEC2")["opportunities"])
    s3=for_service(report,"AmazonS3")
    assert s3["opportunities"]==[] and s3["totals"]["counted_nanos"]==0
    with pytest.raises(QueryError):for_service(report,"unknown-service")
