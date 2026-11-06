"""Offline checks runnable without pytest: python -m unittest tests.test_aws_cur_stdlib."""
import csv
import gzip
import io
import tempfile
import unittest
from pathlib import Path

from costlens.ingest.aws_cur import LocalAwsCurDelivery, normalize_row
from costlens.ingest.cur2 import RowError
from costlens.ingest.sources import FileAccessDenied
from costlens.store import connect


class AwsCurChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.conn = connect(self.root / "test.sqlite")
        self.addCleanup(self.conn.close)
        self.row = {
            "identity_line_item_id": "item1", "bill_billing_period_start_date": "2026-10-01T00:00:00Z",
            "bill_payer_account_id": "123456789012", "line_item_usage_account_id": "123456789012",
            "line_item_line_item_type": "Usage", "line_item_usage_start_date": "2026-10-01T00:00:00Z",
            "line_item_usage_end_date": "2026-10-02T00:00:00Z", "line_item_product_code": "AmazonEC2",
            "line_item_usage_type": "BoxUsage", "line_item_usage_amount": "24",
            "line_item_unblended_cost": "12.34", "line_item_currency_code": "USD",
            "product": '{"region_code":"eu-central-1"}',
        }

    def source(self, *, compressed=False, files=None, **metadata):
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(self.row))
        writer.writeheader()
        writer.writerow(self.row)
        key = "data.csv.gz" if compressed else "data.csv"
        data = output.getvalue().encode()
        (self.root / key).write_bytes(gzip.compress(data) if compressed else data)
        fields = dict(export_name="customer-cur", execution_id="run1", billing_period="2026-10",
                      generated_at="2026-11-02T00:00:00Z")
        fields.update(metadata)
        return LocalAwsCurDelivery(self.root, files=files or [key], **fields)

    def test_exact_money_region_provisional_and_duplicate(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                source = self.source(compressed=compressed)
                self.assertIn(source.ingest(self.conn).status, ("active", "duplicate"))
                self.assertEqual(source.ingest(self.conn).status, "duplicate")
                row = self.conn.execute("SELECT * FROM line_items").fetchone()
                self.assertEqual(row["unblended_nanos"], 12_340_000_000)
                self.assertEqual(row["region"], "eu-central-1")
                self.assertEqual(self.conn.execute("SELECT period_complete FROM deliveries").fetchone()[0], 0)

    def test_conflict_and_revision(self):
        self.source().ingest(self.conn)
        self.row["line_item_unblended_cost"] = "10"
        self.assertEqual(self.source().ingest(self.conn).status, "rejected_conflict")
        newer = self.source(execution_id="run2", generated_at="2026-11-03T00:00:00Z")
        self.assertEqual(newer.ingest(self.conn).status, "active")
        self.assertEqual(self.conn.execute("SELECT SUM(unblended_nanos) FROM line_items").fetchone()[0], 10_000_000_000)

    def test_currency_and_missing_file_keep_active_rows(self):
        self.source().ingest(self.conn)
        self.row["line_item_currency_code"] = "EUR"
        with self.assertRaises(RowError):
            self.source()
        with self.assertRaises(FileNotFoundError):
            self.source(files=["missing.csv"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0], 1)

    def test_paths_and_formats_rejected(self):
        for key in ("../outside.csv", str(self.root / "data.csv")):
            with self.subTest(key=key), self.assertRaises(FileAccessDenied):
                self.source(files=[key])
        with self.assertRaises(ValueError):
            self.source(files=["data.parquet"])

    def test_payer_and_export_isolation(self):
        self.source().ingest(self.conn)
        with self.assertRaises(ValueError):
            self.source(export_name="other").ingest(self.conn)
        self.row["bill_payer_account_id"] = "999999999999"
        with self.assertRaises(ValueError):
            self.source().ingest(self.conn)

    def test_missing_commitment_evidence_rejected(self):
        self.row["line_item_line_item_type"] = "SavingsPlanCoveredUsage"
        with self.assertRaises(RowError):
            normalize_row(self.row, "2026-10")

    def test_negative_credit_preserved(self):
        self.row.update(line_item_line_item_type="Credit", line_item_unblended_cost="-1.25")
        self.source().ingest(self.conn)
        self.assertEqual(self.conn.execute("SELECT unblended_nanos FROM line_items").fetchone()[0], -1_250_000_000)

    def test_cli_ingestion(self):
        from costlens.__main__ import main
        self.source()
        self.assertEqual(main(["ingest-aws-cur", "--source", str(self.root), "--file", "data.csv",
                               "--db", str(self.root / "cli.sqlite"), "--export-name", "customer-cur",
                               "--execution-id", "run1", "--billing-period", "2026-10",
                               "--generated-at", "2026-11-02T00:00:00Z"]), 0)


if __name__ == "__main__":
    unittest.main()
