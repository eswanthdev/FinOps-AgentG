import hashlib
import json
from pathlib import Path

import pytest

from costlens.ingest.loader import ingest_delivery, ingest_source
from costlens.ingest.sources import (DeliveryRef, FileAccessDenied, LocalDirectorySource, S3ExportSource,
                                     SourceUnavailable)
from tests.conftest import copy_deliveries, total_nanos


def statuses(outcomes):
    return [(o.billing_period, o.status) for o in outcomes]


def test_full_fixture_sequence(fresh_conn, fixture_root):
    out = ingest_source(fresh_conn, LocalDirectorySource(fixture_root))
    assert statuses(out) == [
        ("2026-08", "active"), ("2026-09", "active"), ("2026-09", "active"), ("2026-10", "active"),
        ("2026-10", "duplicate"), ("2026-11", "active"), ("2026-11", "rejected_incomplete"),
    ]
    assert "revision of 2026-09" in out[2].detail
    st = {r["billing_period"]: r["n"] for r in fresh_conn.execute(
        "SELECT billing_period, COUNT(*) n FROM deliveries WHERE status='active' GROUP BY 1")}
    assert st == {"2026-08": 1, "2026-09": 1, "2026-10": 1, "2026-11": 1}


def test_duplicate_delivery_is_not_double_counted(fresh_conn, fixture_root, tmp_path):
    single = copy_deliveries(fixture_root, tmp_path / "a", ["004-2026-10-initial"])
    ingest_source(fresh_conn, LocalDirectorySource(single))
    once = total_nanos(fresh_conn, "2026-10")
    both = copy_deliveries(fixture_root, tmp_path / "b", ["005-2026-10-redelivery"])
    out = ingest_source(fresh_conn, LocalDirectorySource(both))
    assert out[0].status == "duplicate"
    assert total_nanos(fresh_conn, "2026-10") == once


def test_reingesting_whole_folder_is_idempotent(fresh_conn, fixture_root):
    ingest_source(fresh_conn, LocalDirectorySource(fixture_root))
    before = total_nanos(fresh_conn)
    rows = fresh_conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]
    out = ingest_source(fresh_conn, LocalDirectorySource(fixture_root))
    assert {o.status for o in out} <= {"duplicate", "rejected_incomplete"}
    assert total_nanos(fresh_conn) == before
    assert fresh_conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == rows


def test_revision_replaces_period(fresh_conn, fixture_root, tmp_path):
    src = copy_deliveries(fixture_root, tmp_path / "s", ["002-2026-09-initial", "003-2026-09-revised"])
    out = ingest_source(fresh_conn, LocalDirectorySource(src))
    assert [o.status for o in out] == ["active", "active"]
    revised_total = fresh_conn.execute(
        "SELECT unblended_total_nanos FROM deliveries WHERE execution_id='exec-2026-09-b'").fetchone()[0]
    assert total_nanos(fresh_conn, "2026-09") == revised_total
    assert fresh_conn.execute("SELECT status FROM deliveries WHERE execution_id='exec-2026-09-a'").fetchone()[0] == "superseded"
    assert fresh_conn.execute("SELECT COUNT(*) FROM line_items WHERE line_item_type='Credit'").fetchone()[0] == 1


def test_older_delivery_after_newer_is_stale(fresh_conn, fixture_root, tmp_path):
    src = copy_deliveries(fixture_root, tmp_path / "s", ["003-2026-09-revised", "002-2026-09-initial"])
    out = ingest_source(fresh_conn, LocalDirectorySource(src))
    assert [o.status for o in out] == ["active", "stale"]
    revised_total = fresh_conn.execute(
        "SELECT unblended_total_nanos FROM deliveries WHERE execution_id='exec-2026-09-b'").fetchone()[0]
    assert total_nanos(fresh_conn, "2026-09") == revised_total


def test_incomplete_delivery_keeps_previous_data(fresh_conn, fixture_root, tmp_path):
    src = copy_deliveries(fixture_root, tmp_path / "s", ["006-2026-11-mtd", "007-2026-11-incomplete"])
    out = ingest_source(fresh_conn, LocalDirectorySource(src))
    assert [o.status for o in out] == ["active", "rejected_incomplete"]
    assert fresh_conn.execute(
        "SELECT execution_id FROM deliveries WHERE billing_period='2026-11' AND status='active'").fetchone()[0] == "exec-2026-11-a"


def _first_folder(tmp_path, fixture_root, name="004-2026-10-initial") -> Path:
    return copy_deliveries(fixture_root, tmp_path / "s", [name])


def test_truncated_file_is_rejected(fresh_conn, fixture_root, tmp_path):
    src = _first_folder(tmp_path, fixture_root)
    f = next(src.iterdir()) / "part-00001.csv"
    f.write_bytes(f.read_bytes()[:-500])
    out = ingest_source(fresh_conn, LocalDirectorySource(src))
    assert out[0].status == "rejected_incomplete" and "checksum" in out[0].detail
    assert total_nanos(fresh_conn) == 0


def _rewrite(folder: Path, mutate):
    m = json.loads((folder / "manifest.json").read_text())
    mutate(m, folder)
    (folder / "manifest.json").write_text(json.dumps(m))


def _resign(m, folder):
    for f in m["dataFiles"]:
        data = (folder / f["key"]).read_bytes()
        f["sha256"] = hashlib.sha256(data).hexdigest()
        f["rowCount"] = data.decode().count("\n") - 1


def test_row_count_mismatch_is_rejected(fresh_conn, fixture_root, tmp_path):
    folder = next(_first_folder(tmp_path, fixture_root).iterdir())
    _rewrite(folder, lambda m, _: m["dataFiles"][0].update(rowCount=m["dataFiles"][0]["rowCount"] + 1))
    out = ingest_source(fresh_conn, LocalDirectorySource(folder.parent))
    assert out[0].status == "rejected_incomplete" and "rows" in out[0].detail


def test_invalid_row_loads_nothing(fresh_conn, fixture_root, tmp_path):
    folder = next(_first_folder(tmp_path, fixture_root).iterdir())
    p = folder / "part-00002.csv"
    lines = p.read_text().splitlines()
    lines[5] = lines[5].replace("us-east-1", "us-east-1", 1)
    cells = lines[5].split(",")
    cells[13] = "not-a-number"  # line_item_unblended_cost
    lines[5] = ",".join(cells)
    p.write_text("\n".join(lines) + "\n")
    _rewrite(folder, _resign)
    out = ingest_source(fresh_conn, LocalDirectorySource(folder.parent))
    assert out[0].status == "rejected_invalid"
    assert total_nanos(fresh_conn) == 0


def test_duplicate_line_ids_inside_delivery_rejected(fresh_conn, fixture_root, tmp_path):
    folder = next(_first_folder(tmp_path, fixture_root).iterdir())
    p = folder / "part-00001.csv"
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines + [lines[1]]) + "\n")
    _rewrite(folder, _resign)
    out = ingest_source(fresh_conn, LocalDirectorySource(folder.parent))
    assert out[0].status == "rejected_invalid" and "duplicate identity_line_item_id" in out[0].detail


def test_same_execution_id_different_content_is_conflict(fresh_conn, fixture_root, tmp_path):
    src = copy_deliveries(fixture_root, tmp_path / "s", ["004-2026-10-initial", "005-2026-10-redelivery"])
    second = sorted(src.iterdir())[1]
    p = second / "part-00001.csv"
    p.write_text(p.read_text().replace("prod-commerce", "prod-commerce-renamed"))
    _rewrite(second, _resign)
    out = ingest_source(fresh_conn, LocalDirectorySource(src))
    assert [o.status for o in out] == ["active", "rejected_conflict"]


def test_bad_manifest_rejected(fresh_conn, fixture_root, tmp_path):
    folder = next(_first_folder(tmp_path, fixture_root).iterdir())
    _rewrite(folder, lambda m, _: m.update(schemaVersion="cur2-v99"))
    out = ingest_source(fresh_conn, LocalDirectorySource(folder.parent))
    assert out[0].status == "rejected_invalid" and "schemaVersion" in out[0].detail


def test_manifest_path_traversal_refused(fresh_conn, fixture_root, tmp_path):
    folder = next(_first_folder(tmp_path, fixture_root).iterdir())
    _rewrite(folder, lambda m, _: m["dataFiles"][0].update(key="../../etc/passwd"))
    out = ingest_source(fresh_conn, LocalDirectorySource(folder.parent))
    assert out[0].status == "rejected_access" and "escapes" in out[0].detail


def test_permission_denied_on_data_file(fresh_conn, fixture_root, tmp_path, monkeypatch):
    src = _first_folder(tmp_path, fixture_root)
    real = Path.read_bytes

    def deny(self):
        if self.name == "part-00002.csv":
            raise PermissionError("denied")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", deny)
    out = ingest_source(fresh_conn, LocalDirectorySource(src))
    assert out[0].status == "rejected_access" and "permission denied" in out[0].detail
    assert total_nanos(fresh_conn) == 0


class DeniedSource:
    name = "denied"

    def list_deliveries(self):
        return [DeliveryRef("s3://customer-bucket/exports/x")]

    def read_manifest(self, ref):
        raise FileAccessDenied("AccessDenied on manifest")

    def read_data_file(self, ref, key):
        raise FileAccessDenied("AccessDenied")


def test_source_level_access_denied_is_recorded(fresh_conn):
    o = ingest_delivery(fresh_conn, DeniedSource(), DeliveryRef("s3://customer-bucket/exports/x"))
    assert o.status == "rejected_access"
    assert fresh_conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='ingest.rejected_access'").fetchone()[0] == 1


def test_missing_source_folder():
    with pytest.raises(SourceUnavailable):
        LocalDirectorySource("/nonexistent/costlens").list_deliveries()


def test_s3_source_is_not_implemented():
    with pytest.raises(NotImplementedError):
        S3ExportSource(bucket="x")
