import pytest

pytest.importorskip("pyarrow")
from costlens.parquet_fixtures import generate
from costlens.ingest.optimization_report import build


def test_cloud_report_restores_validated_details_from_parquet(tmp_path):
    generate(tmp_path)
    report=build(tmp_path)
    assert report["state"]=="ok"
    assert report["totals"]["counted_display"]=="$3,272.06"
    assert report["workflow_read_only"] is True
    assert report["synthetic_data"] is True
    assert len(report["source_provenance"])==5
    assert all(entry["sha256"] for entry in report["source_provenance"])
    ready=[finding for finding in report["opportunities"] if finding["phase"]=="plan_ready"]
    assert ready and all(finding["evidence"] and finding["calculation"]["steps"] and finding["action_plan"] for finding in ready)
    assert {f["phase"] for f in report["opportunities"]}>={"plan_ready","conditional","proposal","alternative","needs_evidence"}
