import hashlib
import json
from decimal import Decimal

import pytest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq

from costlens import fixtures
from costlens.parquet_fixtures import SOURCES, generate
from costlens.ingest.cur2 import parse_row


def test_six_sources_roundtrip_and_reconcile(tmp_path):
    summary = generate(tmp_path)
    assert set(summary["sources"]) == set(SOURCES)
    assert summary["parquet_files"] == 9
    for source in SOURCES:
        manifest = json.loads((tmp_path / source / "manifest.json").read_text())
        assert manifest["synthetic_data"] is True
        for entry in manifest["files"]:
            path = tmp_path / source / entry["key"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
            table = pq.ParquetFile(path).read()
            assert len(table) == entry["row_count"]
            assert table.schema.metadata[b"synthetic_data"] == b"true"
            rows = table.to_pylist()
            assert all(row["synthetic_data"] is True for row in rows)
            if source == "cur":
                assert pa.types.is_decimal(table.schema.field("line_item_unblended_cost").type)
                normalized = [{k: (str(v) if v is not None else "") for k, v in r.items()} for r in rows]
                total = sum(parse_row(r, entry["billing_period"]).unblended_nanos for r in normalized)
                assert str(total) == entry["unblended_total_nanos"]
                if entry["billing_period"] == "2026-10":
                    assert (Decimal(total) / Decimal(1_000_000_000)).quantize(Decimal("0.01")) == Decimal("18561.93")


def test_no_overwrite(tmp_path):
    (tmp_path / "existing.txt").write_text("keep")
    with pytest.raises(ValueError, match="never overwritten"):
        generate(tmp_path)
    assert (tmp_path / "existing.txt").read_text() == "keep"


def test_recommendations_share_original_resource_ids(tmp_path):
    generate(tmp_path)
    recs = pq.ParquetFile(next((tmp_path / "cost-optimization-hub").rglob("*.parquet"))).read().to_pylist()
    assert {r["recommendation_id"] for r in recs} == {r["recommendation_id"] for r in fixtures.coh_rows()}
    ta = pq.ParquetFile(next((tmp_path / "trusted-advisor").rglob("*.parquet"))).read().to_pylist()
    assert all(r["linked_coh_recommendation_id"] in {x["recommendation_id"] for x in recs} for r in ta)


def test_parquet_to_engine_and_repeat_ingestion(tmp_path):
    from costlens.ingest.parquet_snapshot import ingest
    from costlens.agent import optimize
    from costlens.store import connect, get_meta
    root = tmp_path / "snapshots"
    generate(root)
    db = tmp_path / "app.sqlite"
    result = ingest(root, db)
    assert all(o["status"] == "active" for o in result["outcomes"])
    conn = connect(db)
    try:
        assert get_meta(conn, "data_mode") == "synthetic"
        assert conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 3863
        summary = optimize.summary(conn)
        assert summary["totals"]["counted_display"] == "$3,272.06"
    finally:
        conn.close()
    repeat = ingest(root, db)
    assert all(o["status"] == "duplicate" for o in repeat["outcomes"])


def test_corrupt_parquet_prevents_billing_mutation(tmp_path):
    from costlens.ingest.parquet_snapshot import ingest
    root = tmp_path / "snapshots"
    generate(root)
    path = next((root / "trusted-advisor").rglob("*.parquet"))
    path.write_bytes(b"corrupted")
    db = tmp_path / "app.sqlite"
    with pytest.raises(ValueError, match="checksum"):
        ingest(root, db)
    assert not db.exists()
