"""Build the rich Optimize report from verified synthetic S3 snapshot files.

This is a deterministic published analysis, not a fresh AWS recommendation scan.
The deployment package carries it so the UI does not substitute local demo data.
"""
import json
import tempfile
from pathlib import Path

from .parquet_snapshot import ingest, read_source, SOURCE_NAMES
from ..agent.optimize import summary
from ..store import connect, utc_now


def build(root):
    root=Path(root)
    sources={name:read_source(root,name)[0] for name in SOURCE_NAMES}
    with tempfile.TemporaryDirectory(prefix="costlens-optimize-report-") as temporary:
        db=Path(temporary)/"report.sqlite"
        ingest(root,db)
        conn=connect(db)
        try:
            report=summary(conn)
        finally:
            conn.close()
    report.update(backend="cloud",synthetic_data=True,workflow_read_only=True,
        report_generated_at=utc_now(),source_snapshot_as_of=sources["cur"]["as_of"],
        source_provenance=[{"source":name,"sha256":[entry["sha256"] for entry in manifest["files"]]} for name,manifest in sources.items()])
    registrations=json.loads((root/"s3-destinations.json").read_text(encoding="utf-8-sig")) if (root/"s3-destinations.json").exists() else []
    for entry in report["source_provenance"]:
        location_path=root/entry["source"]/"source-location.json"
        location=json.loads(location_path.read_text(encoding="utf-8-sig")) if location_path.exists() else next((r for r in registrations if r["source"]==entry["source"]),None)
        entry["raw_location"]=f"s3://{location['bucket']}/{location['prefix']}/" if location else None
    report["snapshot"]["notes"].append("Calculated from the verified synthetic S3 export snapshot. Planning decisions and implementation recording are not connected in cloud mode yet.")
    return json.loads(json.dumps(report,default=str))
