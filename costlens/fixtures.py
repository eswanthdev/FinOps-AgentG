"""Deterministic synthetic CUR 2.0 deliveries for local mode.

SYNTHETIC DATA. Account ids, names, resources and prices are invented and only
resemble AWS. Same output on every run (no wall-clock or global randomness).

Delivery sequence written (folder names sort in arrival order):
  001-2026-08-final        August, final (periodComplete=true)
  002-2026-09-initial      September, first export
  003-2026-09-revised      September revision: adds a credit, corrects NAT bytes
  004-2026-10-initial      October, first export
  005-2026-10-redelivery   byte-identical repeat of 004 (same execution id)
  006-2026-11-mtd          November month-to-date (through 2 Nov)
  007-2026-11-incomplete   newer November export with one listed file missing
July 2026 is intentionally absent so coverage reporting can show a missing period.

Built-in cost stories (useful for demos and tests):
  * prod-data launches an 8 x c6i.4xlarge ETL fleet on 15 Oct and its NAT
    Gateway data processing jumps at the same time.
  * sandbox has an untagged g5.xlarge GPU instance running since 20 Sep.
  * prod-commerce has a Compute Savings Plan covering 4 m6i.xlarge instances.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Callable

from .ingest.cur2 import REQUIRED_COLUMNS
from .ingest.loader import SCHEMA_VERSION

EXPORT_NAME = "org-cur2-daily"
PAYER = "110987654321"
ACCOUNTS = {
    "210987654321": "prod-commerce",
    "310987654321": "prod-data",
    "410987654321": "staging",
    "510987654321": "sandbox",
}
EXPECTED_START_PERIOD = "2026-07"
DATA_START = date(2026, 8, 1)
DATA_END_INCLUSIVE = date(2026, 11, 2)
REFERENCE_DATE = date(2026, 11, 3)

_Q = Decimal("0.0000000001")


def _d(x) -> Decimal:
    return Decimal(str(x))


def _noise(*parts: object, spread: float = 0.06) -> Decimal:
    """Deterministic multiplier in [1-spread, 1+spread] from a hash of the inputs."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    frac = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
    return _d(1 - spread + 2 * spread * frac)


def _days_in_month(d: date) -> int:
    nxt = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
    return (nxt - date(d.year, d.month, 1)).days


@dataclass(frozen=True)
class Resource:
    account: str
    service: str
    usage_type: str
    operation: str
    resource_id: str
    region: str
    tags: dict
    # returns (usage_amount, unblended_usd) for a day, or None if not running
    daily: Callable[[date], tuple[Decimal, Decimal] | None]
    sp_covered: bool = False
    sp_rate: Decimal = Decimal("0")


def _hours(rate: float, start: date | None = None, end: date | None = None):
    def f(d: date):
        if start and d < start:
            return None
        if end and d > end:
            return None
        return _d(24), _d(rate) * 24
    return f


def _variable(base: float, unit_price: float, key: str, *, weekday_dip: float = 0.0,
              step_from: date | None = None, step_factor: float = 1.0):
    def f(d: date):
        qty = _d(base) * _noise(key, d)
        if weekday_dip and d.weekday() >= 5:
            qty *= _d(1 - weekday_dip)
        if step_from and d >= step_from:
            qty *= _d(step_factor)
        return qty, qty * _d(unit_price)
    return f


def _storage(gb_start: float, daily_growth: float, price_gb_month: float, key: str):
    def f(d: date):
        n = (d - DATA_START).days
        gb = _d(gb_start) * (_d(1 + daily_growth) ** n) * _noise(key, d, spread=0.002)
        return gb / _days_in_month(d), gb * _d(price_gb_month) / _days_in_month(d)
    return f


def build_resources() -> list[Resource]:
    pc, pd, st, sb = "210987654321", "310987654321", "410987654321", "510987654321"
    r: list[Resource] = []
    checkout = {"user:team": "checkout", "user:env": "prod", "user:app": "checkout-api"}
    for i in range(6):
        r.append(Resource(pc, "AmazonEC2", "BoxUsage:m6i.xlarge", "RunInstances", f"i-0c0a{i:04d}m6i",
                          "us-east-1", checkout, _hours(0.192), sp_covered=i < 4, sp_rate=_d("0.1344")))
    web = {"user:team": "checkout", "user:env": "prod", "user:app": "checkout-web"}
    for i in range(4):
        r.append(Resource(pc, "AmazonEC2", "APS3-BoxUsage:c6i.large", "RunInstances", f"i-0c0b{i:04d}c6i",
                          "ap-south-1", web, _hours(0.0910)))
    r += [
        Resource(pc, "AmazonRDS", "RDS:Multi-AZUsage:db.r6g.xlarge", "CreateDBInstance:0016", "db-checkout-prod",
                 "us-east-1", checkout, _hours(0.904)),
        Resource(pc, "AmazonS3", "TimedStorage-ByteHrs", "StandardStorage", "checkout-assets-prod",
                 "us-east-1", checkout, _storage(40_000, 0.004, 0.023, "s3pc")),
        Resource(pc, "AmazonCloudWatch", "DataProcessing-Bytes", "PutLogEvents", "/aws/checkout/api",
                 "us-east-1", checkout, _variable(36, 0.50, "cwpc")),
        Resource(pc, "AWSLambda", "Request", "Invoke", "checkout-webhooks",
                 "us-east-1", checkout, _variable(30, 0.20, "lampc", weekday_dip=0.3)),
        Resource(pc, "AmazonDynamoDB", "PayPerRequestThroughput", "PayPerRequest", "orders-table",
                 "us-east-1", checkout, _variable(20, 1.25, "ddbpc", weekday_dip=0.35)),
        Resource(pc, "AmazonEC2", "NatGateway-Hours", "NatGateway", "nat-0pc1",
                 "us-east-1", checkout, _hours(0.045)),
        Resource(pc, "AmazonEC2", "NatGateway-Bytes", "NatGateway", "nat-0pc1",
                 "us-east-1", checkout, _variable(600, 0.045, "natpc")),
    ]
    data = {"user:team": "data-platform", "user:env": "prod", "user:app": "warehouse"}
    etl = {"user:team": "data-platform", "user:env": "prod", "user:app": "etl-batch"}
    for i in range(3):
        r.append(Resource(pd, "AmazonEC2", "BoxUsage:r6i.2xlarge", "RunInstances", f"i-0d0a{i:04d}r6i",
                          "us-east-1", data, _hours(0.504)))
    for i in range(8):
        r.append(Resource(pd, "AmazonEC2", "BoxUsage:c6i.4xlarge", "RunInstances", f"i-0d0e{i:04d}c6i",
                          "us-east-1", etl, _hours(0.680, start=date(2026, 10, 15))))
    r += [
        Resource(pd, "AmazonS3", "TimedStorage-ByteHrs", "StandardStorage", "datalake-raw",
                 "us-east-1", data, _storage(180_000, 0.003, 0.023, "s3pd")),
        Resource(pd, "AWSGlue", "Crawler-DPU-Hour", "CrawlerRun", "glue-crawlers",
                 "us-east-1", data, _variable(90, 0.44, "gluepd")),
        Resource(pd, "AmazonEC2", "NatGateway-Hours", "NatGateway", "nat-0pd1",
                 "us-east-1", data, _hours(0.045)),
        Resource(pd, "AmazonEC2", "NatGateway-Bytes", "NatGateway", "nat-0pd1",
                 "us-east-1", etl, _variable(300, 0.045, "natpd", step_from=date(2026, 10, 15), step_factor=8.0)),
    ]
    stg = {"user:env": "staging", "user:team": "checkout", "user:app": "checkout-api"}
    r.append(Resource(st, "AmazonRDS", "EU-InstanceUsage:db.m6g.large", "CreateDBInstance:0016", "db-checkout-stg",
                      "eu-west-1", stg, _hours(0.172)))
    for i in range(5):
        r.append(Resource(st, "AmazonEC2", "EU-BoxUsage:t3.large", "RunInstances", f"i-0e0a{i:04d}t3",
                          "eu-west-1", stg, _hours(0.0912)))
    r.append(Resource(st, "AmazonEC2", "EU-EBS:VolumeUsage.gp2", "CreateVolume-Gp2", "vol-stg-pool",
                      "eu-west-1", stg, _storage(2_000, 0.0, 0.11, "ebsst")))
    r.append(Resource(sb, "AmazonEC2", "BoxUsage:g5.xlarge", "RunInstances", "i-0f0a0001g5",
                      "us-east-1", {}, _hours(1.006, start=date(2026, 9, 20))))
    for i in range(2):
        r.append(Resource(sb, "AmazonEC2", "BoxUsage:t3.medium", "RunInstances", f"i-0f0b{i:04d}t3",
                          "us-east-1", {"user:env": "dev"} if i == 0 else {}, _hours(0.0416)))
    r.append(Resource(sb, "AWSLambda", "Request", "Invoke", "sandbox-experiments",
                      "us-east-1", {}, _variable(4, 0.20, "lamsb")))
    return r


SP_DAILY_COMMITMENT = _d("14.00")
SP_ARN = "arn:aws:savingsplans::210987654321:savingsplan/sp-0synthetic0001"


def _q(x: Decimal) -> str:
    return str(x.quantize(_Q, rounding=ROUND_HALF_EVEN))


def _line_id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


def _row(period_start: date, account: str, line_type: str, d: date, service: str, usage_type: str,
         operation: str, resource_id: str, region: str, usage: Decimal, unblended: Decimal,
         on_demand: Decimal, tags: dict, *, sp_eff: Decimal = Decimal(0), sp_total: Decimal = Decimal(0),
         sp_used: Decimal = Decimal(0)) -> dict:
    return {
        "identity_line_item_id": _line_id(period_start, account, line_type, d, resource_id, usage_type),
        "bill_billing_period_start_date": f"{period_start.isoformat()}T00:00:00Z",
        "bill_payer_account_id": PAYER,
        "line_item_usage_account_id": account,
        "line_item_usage_account_name": ACCOUNTS[account],
        "line_item_line_item_type": line_type,
        "line_item_usage_start_date": f"{d.isoformat()}T00:00:00Z",
        "line_item_usage_end_date": f"{(d + timedelta(days=1)).isoformat()}T00:00:00Z",
        "line_item_product_code": service,
        "line_item_usage_type": usage_type,
        "line_item_operation": operation,
        "line_item_resource_id": resource_id,
        "line_item_usage_amount": _q(usage),
        "line_item_unblended_cost": _q(unblended),
        "pricing_public_on_demand_cost": _q(on_demand),
        "product_region_code": region,
        "savings_plan_savings_plan_effective_cost": _q(sp_eff),
        "savings_plan_total_commitment_to_date": _q(sp_total),
        "savings_plan_used_commitment": _q(sp_used),
        "resource_tags": json.dumps(tags, sort_keys=True),
    }


def rows_for_period(period: str, through: date, *, revision: bool = False) -> list[dict]:
    y, m = map(int, period.split("-"))
    p_start = date(y, m, 1)
    out: list[dict] = []
    resources = build_resources()
    d = p_start
    while d.month == m and d <= through:
        sp_used = Decimal(0)
        for res in resources:
            v = res.daily(d)
            if v is None:
                continue
            usage, cost = v
            if revision and res.usage_type == "NatGateway-Bytes" and res.account == "310987654321":
                cost *= _d("1.03")  # corrected metering in the revised export
            if res.sp_covered:
                eff = res.sp_rate * usage
                sp_used += eff
                out.append(_row(p_start, res.account, "SavingsPlanCoveredUsage", d, res.service, res.usage_type,
                                res.operation, res.resource_id, res.region, usage, cost, cost, res.tags, sp_eff=eff))
                out.append(_row(p_start, res.account, "SavingsPlanNegation", d, res.service, res.usage_type,
                                res.operation, res.resource_id, res.region, Decimal(0), -cost, Decimal(0), res.tags))
            else:
                out.append(_row(p_start, res.account, "Usage", d, res.service, res.usage_type, res.operation,
                                res.resource_id, res.region, usage, cost, cost, res.tags))
        out.append(_row(p_start, "210987654321", "SavingsPlanRecurringFee", d, "ComputeSavingsPlans",
                        "ComputeSP:1yrNoUpfront", "", SP_ARN, "global", Decimal(1), SP_DAILY_COMMITMENT,
                        Decimal(0), {}, sp_total=SP_DAILY_COMMITMENT, sp_used=sp_used))
        d += timedelta(days=1)
    if revision:
        last = d - timedelta(days=1)
        out.append(_row(p_start, "310987654321", "Credit", last, "AmazonEC2", "Credit", "", "",
                        "us-east-1", Decimal(0), _d("-250.00"), Decimal(0), {}))
    return out


def _write_delivery(root: Path, folder: str, period: str, execution_id: str, exported: str,
                    rows: list[dict], *, complete: bool, drop_file: bool = False) -> None:
    out = root / folder
    out.mkdir(parents=True, exist_ok=True)
    parts = {
        "part-00001.csv": [r for r in rows if r["line_item_usage_account_id"] in ("210987654321", "410987654321")],
        "part-00002.csv": [r for r in rows if r["line_item_usage_account_id"] in ("310987654321", "510987654321")],
    }
    files = []
    for key, part_rows in parts.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(REQUIRED_COLUMNS), lineterminator="\n")
        w.writeheader()
        w.writerows(part_rows)
        data = buf.getvalue().encode("utf-8")
        files.append({"key": key, "sha256": hashlib.sha256(data).hexdigest(), "rowCount": len(part_rows)})
        if not (drop_file and key == "part-00002.csv"):
            (out / key).write_bytes(data)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "syntheticData": True,
        "exportName": EXPORT_NAME,
        "executionId": execution_id,
        "billingPeriod": period,
        "exportTimestamp": exported,
        "periodComplete": complete,
        "dataFiles": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def generate(root: str | Path, *, overwrite: bool = False) -> Path:
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        if not overwrite:
            return root
        if not (root / "SYNTHETIC_FIXTURES").exists():
            raise RuntimeError(f"refusing to overwrite {root}: not a fixture folder created by this tool")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "SYNTHETIC_FIXTURES").write_text(
        "Synthetic CUR 2.0 deliveries generated by costlens.fixtures. Not real AWS data.\n", encoding="utf-8")

    _write_delivery(root, "001-2026-08-final", "2026-08", "exec-2026-08-a", "2026-09-03T04:00:00Z",
                    rows_for_period("2026-08", date(2026, 8, 31)), complete=True)
    _write_delivery(root, "002-2026-09-initial", "2026-09", "exec-2026-09-a", "2026-10-01T06:00:00Z",
                    rows_for_period("2026-09", date(2026, 9, 30)), complete=False)
    _write_delivery(root, "003-2026-09-revised", "2026-09", "exec-2026-09-b", "2026-10-04T04:00:00Z",
                    rows_for_period("2026-09", date(2026, 9, 30), revision=True), complete=True)
    oct_rows = rows_for_period("2026-10", date(2026, 10, 31))
    _write_delivery(root, "004-2026-10-initial", "2026-10", "exec-2026-10-a", "2026-11-01T06:00:00Z",
                    oct_rows, complete=False)
    _write_delivery(root, "005-2026-10-redelivery", "2026-10", "exec-2026-10-a", "2026-11-01T06:00:00Z",
                    oct_rows, complete=False)
    nov_rows = rows_for_period("2026-11", DATA_END_INCLUSIVE)
    _write_delivery(root, "006-2026-11-mtd", "2026-11", "exec-2026-11-a", "2026-11-03T05:00:00Z",
                    nov_rows, complete=False)
    _write_delivery(root, "007-2026-11-incomplete", "2026-11", "exec-2026-11-b", "2026-11-03T09:00:00Z",
                    nov_rows, complete=False, drop_file=True)
    return root
