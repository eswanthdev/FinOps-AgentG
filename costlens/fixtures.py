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

An optimization/ subfolder holds one synthetic Cost Optimization Hub and Compute
Optimizer snapshot tied to the same resources. See docs/DATA_CONTRACTS.md.
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


OPT_FOLDER = "optimization"
HOURS_PER_MONTH = _d(730)
_REFRESHED = "2026-11-02T03:00:00Z"


def _monthly(rate: str) -> str:
    return str(_d(rate) * HOURS_PER_MONTH)


def _ec2_arn(account: str, region: str, instance: str) -> str:
    return f"arn:aws:ec2:{region}:{account}:instance/{instance}"


def _coh_row(rec_id: str, account: str, region: str, arn: str, action: str, current_type: str, current: str,
             recommended_type: str, recommended: str, cost_before: str, cost_after: str, savings_before: str,
             savings_after: str, *, source: str = "ComputeOptimizer", effort: str = "Low", restart: bool = True,
             rollback: bool = True, lookback: int = 14, details: dict | None = None) -> dict:
    return {
        "recommendation_id": rec_id, "account_id": account, "region": region, "resource_arn": arn,
        "action_type": action, "currency_code": "USD", "current_resource_type": current_type,
        "current_resource_summary": current, "recommended_resource_type": recommended_type,
        "recommended_resource_summary": recommended,
        "recommended_resource_details": json.dumps(details or {}, sort_keys=True),
        "recommendation_source": source,
        "estimated_monthly_cost_before_discount": cost_before, "estimated_monthly_cost_after_discount": cost_after,
        "estimated_monthly_savings_before_discount": savings_before,
        "estimated_monthly_savings_after_discount": savings_after,
        "implementation_effort": effort, "restart_needed": str(restart).lower(),
        "rollback_possible": str(rollback).lower(), "recommendation_lookback_period_in_days": str(lookback),
        "last_refresh_timestamp": _REFRESHED,
    }


def _rightsize(rec_id: str, account: str, region: str, instance: str, current: str, target: str,
               rate: str, target_rate: str, *, after_rate: str | None = None, after_target: str | None = None) -> dict:
    """On-demand values by default. after_* are the discounted rates of a Savings Plan covered instance."""
    before = _d(rate) - _d(target_rate)
    after = _d(after_rate) - _d(after_target) if after_rate else before
    return _coh_row(rec_id, account, region, _ec2_arn(account, region, instance), "Rightsize", "Ec2Instance", current,
                    "Ec2Instance", target, _monthly(rate), _monthly(after_rate or rate),
                    str(before * HOURS_PER_MONTH), str(after * HOURS_PER_MONTH))


def coh_rows() -> list[dict]:
    """Synthetic Cost Optimization Hub rows. Each one exercises a rule in agent/optimize.py."""
    pc, pd, st, sb = "210987654321", "310987654321", "410987654321", "510987654321"
    east = "us-east-1"
    rows = []
    # m6i fleet: 0-3 are Savings Plan covered (conflict), 4-5 are on demand (clean).
    for i in range(4):
        rows.append(_rightsize(f"coh-m6i-{i}", pc, east, f"i-0c0a{i:04d}m6i", "m6i.xlarge", "m6i.large",
                               "0.192", "0.096", after_rate="0.1344", after_target="0.0672"))
    for i in (4, 5):
        rows.append(_rightsize(f"coh-m6i-{i}", pc, east, f"i-0c0a{i:04d}m6i", "m6i.xlarge", "m6i.large", "0.192", "0.096"))
    # ETL fleet: launched 15 Oct, so short history; memory metric is missing in Compute Optimizer.
    for i in range(8):
        rows.append(_rightsize(f"coh-etl-{i}", pd, east, f"i-0d0e{i:04d}c6i", "c6i.4xlarge", "c6i.2xlarge", "0.680", "0.340"))
    # Same instances as coh-etl-0/1: a mutually exclusive alternative.
    for i in (0, 1):
        rows.append(_coh_row(f"coh-etl-grav-{i}", pd, east, _ec2_arn(pd, east, f"i-0d0e{i:04d}c6i"), "MigrateToGraviton",
                             "Ec2Instance", "c6i.4xlarge", "Ec2Instance", "c7g.4xlarge", _monthly("0.680"),
                             _monthly("0.680"), _monthly("0.100"), _monthly("0.100"), effort="Medium"))
    rows.append(_rightsize("coh-r6i-0", pd, east, "i-0d0a0000r6i", "r6i.2xlarge", "r6i.xlarge", "0.504", "0.252"))
    rows.append(_coh_row("coh-g5-stop", sb, east, _ec2_arn(sb, east, "i-0f0a0001g5"), "Stop", "Ec2Instance", "g5.xlarge",
                         "Ec2Instance", "Stopped", _monthly("1.006"), _monthly("1.006"), _monthly("1.006"),
                         _monthly("1.006"), effort="VeryLow", restart=False))
    rows.append(_coh_row("coh-ebs-gp3", st, "eu-west-1", f"arn:aws:ec2:eu-west-1:{st}:volume/vol-stg-pool", "Upgrade",
                         "EbsVolume", "gp2, 2000 GiB", "EbsVolume", "gp3, 2000 GiB", "220.00", "220.00", "44.00", "44.00",
                         source="CostOptimizationHub", restart=False))
    # Not enough evidence, each for a different reason.
    rows.append(_rightsize("coh-t3-conflict", st, "eu-west-1", "i-0e0a0000t3", "t3.large", "t3.medium", "0.0912", "0.0456"))
    rows.append(_coh_row("coh-rds-prod", pc, east, f"arn:aws:rds:{east}:{pc}:db:db-checkout-prod", "Rightsize",
                         "RdsDbInstance", "db.r6g.xlarge Multi-AZ", "RdsDbInstance", "db.r6g.large Multi-AZ",
                         _monthly("0.904"), _monthly("0.904"), _monthly("0.452"), _monthly("0.452"), effort="Medium"))
    rows.append(_rightsize("coh-gone", pd, east, "i-0d0e9999gone", "c6i.4xlarge", "c6i.2xlarge", "0.680", "0.340"))
    rows.append(_coh_row("coh-t3-overclaim", sb, east, _ec2_arn(sb, east, "i-0f0b0000t3"), "Stop", "Ec2Instance", "t3.medium",
                         "Ec2Instance", "Stopped", "300.00", "300.00", "300.00", "300.00", effort="VeryLow", restart=False))
    rows.append(_coh_row("coh-t3-short", sb, east, _ec2_arn(sb, east, "i-0f0b0001t3"), "Stop", "Ec2Instance", "t3.medium",
                         "Ec2Instance", "Stopped", _monthly("0.0416"), _monthly("0.0416"), _monthly("0.0416"),
                         _monthly("0.0416"), effort="VeryLow", restart=False, lookback=3))
    # Commitment: sized on today's on-demand usage in prod-data, before any rightsizing.
    rows.append(_coh_row("coh-sp-data", pd, east, "", "PurchaseSavingsPlans", "ComputeSavingsPlans", "On-demand compute",
                         "ComputeSavingsPlans", "$5.00/hour, 1 year, No upfront", "5069.12", "3650.00", "1419.12", "1419.12",
                         source="CostExplorer", effort="VeryLow", restart=False, rollback=False, lookback=30,
                         details={"hourlyCommitment": "5.00", "paymentOption": "NoUpfront", "term": "OneYear"}))
    return rows


def _co_row(account: str, region: str, instance: str, finding: str, current: str, price: str, cpu: str, memory: str,
            target: str = "", risk: str = "", savings: str = "0", effort: str = "") -> dict:
    return {
        "accountId": account, "instanceArn": _ec2_arn(account, region, instance), "finding": finding,
        "currentInstanceType": current, "currentOnDemandPrice": price, "utilizationMetrics_CPU_MAXIMUM": cpu,
        "utilizationMetrics_MEMORY_MAXIMUM": memory, "recommendationOptions_1_instanceType": target,
        "recommendationOptions_1_performanceRisk": risk,
        "recommendationOptions_1_estimatedMonthlySavings_value": savings if target else "",
        "recommendationOptions_1_estimatedMonthlySavings_currency": "USD" if target else "",
        "recommendationOptions_1_migrationEffort": effort, "lookbackPeriodInDays": "14",
        "lastRefreshTimestamp": _REFRESHED,
    }


def co_rows() -> list[dict]:
    """Synthetic Compute Optimizer EC2 rows: the utilisation evidence behind the recommendations."""
    pc, pd, st, sb = "210987654321", "310987654321", "410987654321", "510987654321"
    east = "us-east-1"
    over = "OVER_PROVISIONED"
    rows = [_co_row(pc, east, f"i-0c0a{i:04d}m6i", over, "m6i.xlarge", "0.192", "31.5", "42.0", "m6i.large", "1.0",
                    _monthly("0.096"), "VeryLow") for i in range(6)]
    rows += [_co_row(pd, east, f"i-0d0e{i:04d}c6i", over, "c6i.4xlarge", "0.680", "38.2", "", "c6i.2xlarge", "2.0",
                     _monthly("0.340"), "Low") for i in range(8)]
    rows.append(_co_row(pd, east, "i-0d0a0000r6i", over, "r6i.2xlarge", "0.504", "29.0", "44.0", "r6i.xlarge", "1.0",
                        _monthly("0.252"), "Low"))
    # No matching Cost Optimization Hub row: a Compute Optimizer-only opportunity, with high performance risk.
    rows.append(_co_row(pd, east, "i-0d0a0001r6i", over, "r6i.2xlarge", "0.504", "47.5", "61.0", "r6i.xlarge", "3.0",
                        _monthly("0.252"), "Medium"))
    rows.append(_co_row(pd, east, "i-0d0a0002r6i", "OPTIMIZED", "r6i.2xlarge", "0.504", "71.0", "78.0"))
    rows.append(_co_row(st, "eu-west-1", "i-0e0a0000t3", "OPTIMIZED", "t3.large", "0.0912", "64.0", "70.0"))
    rows.append(_co_row(sb, east, "i-0f0a0001g5", over, "g5.xlarge", "1.006", "2.1", "3.4", "g4dn.xlarge", "1.0",
                        _monthly("0.480"), "Low"))
    rows.append(_co_row(sb, east, "i-0f0b0000t3", over, "t3.medium", "0.0416", "3.0", "9.0", "t3.small", "1.0",
                        _monthly("0.0208"), "VeryLow"))
    rows.append(_co_row(sb, east, "i-0f0b0001t3", over, "t3.medium", "0.0416", "4.0", "8.0", "t3.small", "1.0",
                        _monthly("0.0208"), "VeryLow"))
    return rows


def write_optimization_snapshot(root: str | Path, folder: str, execution_id: str, exported: str,
                                coh: list[dict] | None, co: list[dict] | None) -> Path:
    """Write one snapshot folder. Pass None to leave a table out of the snapshot."""
    from .ingest.optimization import CO_COLUMNS, COH_COLUMNS, OPT_SCHEMA_VERSION
    out = Path(root) / folder
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for key, table, columns, rows in (("coh-recommendations.csv", "coh", COH_COLUMNS, coh),
                                      ("compute-optimizer-ec2.csv", "compute_optimizer_ec2", CO_COLUMNS, co)):
        if rows is None:
            continue
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        data = buf.getvalue().encode("utf-8")
        (out / key).write_bytes(data)
        files.append({"key": key, "sha256": hashlib.sha256(data).hexdigest(), "rowCount": len(rows), "table": table})
    manifest = {"schemaVersion": OPT_SCHEMA_VERSION, "syntheticData": True, "executionId": execution_id,
                "exportTimestamp": exported, "dataFiles": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


def generate_optimization(root: str | Path) -> Path:
    """Add the synthetic optimization snapshot to a fixture folder, if it is not there yet."""
    target = Path(root) / OPT_FOLDER
    if not target.exists():
        write_optimization_snapshot(target, "001-2026-11-02", "opt-2026-11-02", "2026-11-02T06:00:00Z", coh_rows(), co_rows())
    return target


GOV_FOLDER = "governance"


def organization() -> dict:
    """Synthetic organization: the management account holds no workloads."""
    return {
        "schemaVersion": "org-subset-v1", "syntheticData": True, "managementAccount": PAYER,
        "organizationalUnits": [
            {"id": "ou-prod", "name": "Production", "accounts": ["210987654321", "310987654321"]},
            {"id": "ou-nonprod", "name": "Non-production", "accounts": ["410987654321"]},
            {"id": "ou-sandbox", "name": "Sandbox", "accounts": ["510987654321"]},
        ],
        "serviceControlPolicies": [
            {"id": "p-fullaccess", "name": "FullAWSAccess", "targets": ["root"]},
            {"id": "p-denyleave", "name": "DenyLeaveOrganization", "targets": ["ou-prod", "ou-sandbox"]},
        ],
    }


def policies() -> dict:
    return {"schemaVersion": "policy-subset-v1", "policies": [
        {"id": "team-tag", "name": "Every resource has a team tag", "kind": "required_tag", "params": {"key": "user:team"}},
        {"id": "approved-regions", "name": "Only approved regions", "kind": "allowed_regions",
         "params": {"regions": ["us-east-1", "ap-south-1", "eu-west-1"]}},
        {"id": "no-gpu-sandbox", "name": "No GPU instances in sandbox", "kind": "restricted_instance",
         "params": {"accounts": ["510987654321"], "families": ["g4dn", "g5", "g6", "p4d", "p5"]}},
        {"id": "budget-every-account", "name": "Every account has a budget", "kind": "budget_coverage", "params": {}},
    ]}


def generate_governance(root: str | Path) -> Path:
    """Add the synthetic organization and policies to a fixture folder, if they are not there yet."""
    target = Path(root) / GOV_FOLDER
    if not target.exists():
        target.mkdir(parents=True)
        (target / "organization.json").write_text(json.dumps(organization(), indent=2) + "\n", encoding="utf-8")
        (target / "policies.json").write_text(json.dumps(policies(), indent=2) + "\n", encoding="utf-8")
    return target


def generate(root: str | Path, *, overwrite: bool = False) -> Path:
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        if not overwrite:
            if (root / "SYNTHETIC_FIXTURES").exists():
                generate_optimization(root)  # fixture folders made before Optimize existed
                generate_governance(root)
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
    generate_optimization(root)
    generate_governance(root)
    return root
