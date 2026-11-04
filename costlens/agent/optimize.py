"""Cost Optimization Engineer: data quality -> investigation -> optimization -> risk -> action planning.

Deterministic, read-only, local. Nothing here calls AWS or changes anything. Every
finding is an ESTIMATE; realised savings are not measured in this release.

Rules (documented so results are reproducible):
  * Evidence window: the last 30 days of ingested CUR. Monthly run-rate =
    amortized cost x 730 / (days with usage x 24), in integer nano-USD.
  * A savings number is shown only if the evidence supports it. Otherwise the
    finding is "needs_evidence", with no estimate and the source claim kept apart.
  * One resource counts once. Competing recommendations on a resource are mutually
    exclusive: the best supported one is counted, the rest are "alternative".
  * Rightsizing a Savings Plan covered instance is "conditional": the commitment
    is still paid, so its saving is not counted until freed commitment is absorbed.
  * Commitments are "proposal" only and never added to the counted total. Rightsize
    first: the proposal is scaled down by the counted savings in its scope.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from ..money import parse_usd_to_nanos, share_pct
from ..query import engine
from ..query.engine import _fmt_usd, account_names
from ..query.model import CostQuery
from ..store import utc_now
from .orchestrator import AuditLog, Tool

HOURS_PER_MONTH = 730
WINDOW_DAYS = 30
RATE_DAYS = 7
MIN_LOOKBACK_DAYS = 14
MAX_SNAPSHOT_AGE_DAYS = 14
COST_TOLERANCE_PCT = 10
STOP_MAX_CPU = Decimal("10")
MAX_CANDIDATES = 500
STATUSES = ("open", "accepted", "dismissed")
COMMITMENT_ACTIONS = ("PurchaseSavingsPlans", "PurchaseReservedInstances")
UTILISATION_ACTIONS = ("Rightsize", "Stop", "ScaleIn", "MigrateToGraviton")
EFFORT_ORDER = ("VeryLow", "Low", "Medium", "High", "VeryHigh")
CONFIDENCE_ORDER = ("high", "medium", "low")
PHASES = {"plan_ready": "Plan ready", "conditional": "Conditional", "proposal": "Proposal only",
          "needs_evidence": "Needs evidence", "alternative": "Alternative"}
SERVICE_BY_TYPE = {"Ec2Instance": "AmazonEC2", "EbsVolume": "AmazonEC2", "RdsDbInstance": "AmazonRDS",
                   "ComputeSavingsPlans": "ComputeSavingsPlans"}
ACTION_LABELS = {"Rightsize": "Rightsize", "Stop": "Stop", "Delete": "Delete", "Upgrade": "Upgrade",
                 "MigrateToGraviton": "Migrate to Graviton", "ScaleIn": "Scale in",
                 "PurchaseSavingsPlans": "Buy Savings Plan", "PurchaseReservedInstances": "Buy Reserved Instances"}
REALISED = {"state": "not_measured", "display": "Not measured",
            "detail": "No change has been made and savings verification is not built yet, so realised savings are unknown."}


def monthly_run_rate(total_nanos: int, days: int) -> int:
    """Normalise a window total to a 730-hour month. Integer arithmetic only."""
    return total_nanos * HOURS_PER_MONTH // (days * 24) if days > 0 else 0


def scaled(amount_nanos: int, remaining_nanos: int, baseline_nanos: int) -> int:
    """amount x remaining / baseline, never below zero. Integer arithmetic only."""
    if baseline_nanos <= 0:
        return 0
    return max(0, amount_nanos * max(0, remaining_nanos) // baseline_nanos)


def _lowest(current: str, floor: str) -> str:
    return CONFIDENCE_ORDER[max(CONFIDENCE_ORDER.index(current), CONFIDENCE_ORDER.index(floor))]


class _Worker:
    """Owns a fixed allow-list of typed tools. Anything else raises PermissionError."""
    name = ""

    def __init__(self, conn, trace: dict[str, int]):
        self.conn = conn
        self.trace = trace
        self.tools = {t.name: t for t in self._tools()}

    def _tools(self) -> list[Tool]:
        raise NotImplementedError

    def call(self, tool: str, **kwargs):
        if tool not in self.tools:
            raise PermissionError(f"tool {tool!r} is not available to the {self.name} worker")
        key = f"{self.name}.{tool}"
        self.trace[key] = self.trace.get(key, 0) + 1
        return self.tools[tool].handler(**kwargs)


class DataQualityWorker(_Worker):
    name = "data_quality"

    def _tools(self):
        return [Tool("snapshot_status", "Which optimization snapshot is active, and how old it is.", self._snapshot),
                Tool("evidence_window", "The CUR window used as evidence.", self._window),
                Tool("check_evidence", "Reasons a recommendation cannot be trusted yet.", self._check)]

    def _snapshot(self) -> dict:
        row = self.conn.execute("SELECT * FROM opt_deliveries WHERE status='active'").fetchone()
        rejected = self.conn.execute(
            "SELECT status, detail, execution_id FROM opt_deliveries WHERE status LIKE 'rejected_%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        notes = [f"A snapshot was rejected ({rejected['status']}): {rejected['detail']}"] if rejected else []
        if not row:
            return {"state": "no_data", "notes": notes}
        age = (engine.reference_date(self.conn) - date.fromisoformat(row["export_timestamp"][:10])).days
        if age > MAX_SNAPSHOT_AGE_DAYS:
            notes.append(f"The snapshot is {age} days old. Recommendations may no longer match what is running.")
        return {"state": "stale" if age > MAX_SNAPSHOT_AGE_DAYS else "ok", "execution_id": row["execution_id"],
                "exported": row["export_timestamp"], "age_days": age, "coh_rows": row["coh_rows"],
                "co_rows": row["co_rows"], "notes": notes}

    def _window(self) -> dict | None:
        last = self.conn.execute("SELECT MAX(usage_date) AS d FROM line_items").fetchone()["d"]
        if not last:
            return None
        through = date.fromisoformat(last)
        start, end = through - timedelta(days=WINDOW_DAYS - 1), through + timedelta(days=1)
        return {"start": start, "end": end, "days": WINDOW_DAYS, "data_through": last,
                "notes": engine.coverage(self.conn, start, end)["notes"]}

    def _check(self, cand: dict, facts: dict, util: dict | None) -> list[str]:
        blockers = []
        if cand["savings_nanos"] <= 0:
            blockers.append("The source shows no savings for this recommendation.")
        if cand["lookback_days"] < MIN_LOOKBACK_DAYS:
            blockers.append(f"The recommendation looks at only {cand['lookback_days']} days. At least "
                            f"{MIN_LOOKBACK_DAYS} are needed.")
        if not facts["found"]:
            blockers.append("This resource has no cost in the CUR evidence window. It may already be gone, "
                            "or the recommendation is out of date.")
        elif facts["unsupported_rows"]:
            blockers.append("Some charges on this resource cannot be amortized in this release, so its true cost is unknown.")
        elif cand["savings_nanos"] > facts["monthly_nanos"]:
            blockers.append(f"The source claims {_fmt_usd(cand['savings_nanos'])} a month, more than the "
                            f"{_fmt_usd(facts['monthly_nanos'])} this resource costs.")
        if cand["action"] in UTILISATION_ACTIONS:
            blockers += self._utilisation_blockers(cand, util)
        return blockers

    @staticmethod
    def _utilisation_blockers(cand: dict, util: dict | None) -> list[str]:
        if util is None:
            return ["No utilisation evidence for this resource. Only the Compute Optimizer EC2 export is ingested."]
        if util["finding"] != "OVER_PROVISIONED":
            return [f"Sources disagree: Compute Optimizer says {util['finding']}, so this {cand['action']} "
                    "recommendation is not supported."]
        if cand["action"] == "Stop" and (not util["cpu_max"] or Decimal(util["cpu_max"]) > STOP_MAX_CPU):
            return [f"Stopping needs peak CPU of {STOP_MAX_CPU}% or less. Evidence shows "
                    f"{util['cpu_max'] + '%' if util['cpu_max'] else 'no CPU metric'}."]
        return []


class InvestigationWorker(_Worker):
    name = "investigation"

    def _tools(self):
        return [Tool("resource_facts", "CUR cost and usage facts for one resource.", self._facts),
                Tool("utilisation", "Compute Optimizer evidence for one instance.", self._utilisation),
                Tool("commitment_usage", "How much of the existing Savings Plan commitment is used.", self._commitment),
                Tool("on_demand_rate", "On-demand instance run-rate for one account.", self._on_demand)]

    def _facts(self, account_id: str, resource_id: str, window: dict) -> dict:
        q = CostQuery(start=window["start"], end=window["end"], metric="amortized", granularity="daily",
                      group_by="usage_type", filters={"account": [account_id], "resource": [resource_id]},
                      chart="stacked").validate()
        where, params = engine._where(q, q.start, q.end)
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT usage_date) AS days, COALESCE(SUM(line_item_type='SavingsPlanCoveredUsage'),0) AS covered, "
            "COALESCE(SUM(amortized_nanos IS NULL),0) AS unsupported, MAX(resource_tags) AS tags, MIN(usage_type) AS usage_type "
            f"FROM line_items WHERE {where}", params).fetchone()
        total = engine._window_total(self.conn, q, q.start, q.end)
        days = row["days"]
        return {"found": days > 0, "days": days, "amortized_nanos": total, "monthly_nanos": monthly_run_rate(total, days),
                "sp_covered": row["covered"] > 0, "unsupported_rows": row["unsupported"],
                "tags": json.loads(row["tags"] or "{}"), "usage_type": row["usage_type"], "query": q.to_dict()}

    def _utilisation(self, account_id: str, resource_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM co_ec2_recommendations WHERE account_id=? AND resource_id=?",
                                (account_id, resource_id)).fetchone()
        return dict(row) if row else None

    def _commitment(self, window: dict) -> dict:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(unblended_nanos),0) AS fee, COALESCE(SUM(amortized_nanos),0) AS unused FROM line_items "
            "WHERE line_item_type='SavingsPlanRecurringFee' AND usage_date >= ? AND usage_date < ?",
            (window["start"].isoformat(), window["end"].isoformat())).fetchone()
        return {"fee_nanos": row["fee"], "unused_nanos": row["unused"],
                "unused_pct": share_pct(row["unused"], row["fee"])}

    def _on_demand(self, account_id: str, window: dict) -> dict:
        start, end = window["end"] - timedelta(days=RATE_DAYS), window["end"]
        types = [r["usage_type"] for r in self.conn.execute(
            "SELECT DISTINCT usage_type FROM line_items WHERE account_id=? AND line_item_type='Usage' "
            "AND usage_type LIKE '%BoxUsage%' AND usage_date >= ? AND usage_date < ? ORDER BY usage_type LIMIT 50",
            (account_id, start.isoformat(), end.isoformat()))]
        if not types:
            return {"found": False, "monthly_nanos": 0, "query": None, "start": start, "end": end}
        q = CostQuery(start=start, end=end, metric="unblended", granularity="daily", group_by="usage_type",
                      filters={"account": [account_id], "line_item_type": ["Usage"], "usage_type": types},
                      chart="stacked").validate()
        total = engine._window_total(self.conn, q, start, end)
        return {"found": True, "monthly_nanos": monthly_run_rate(total, RATE_DAYS), "total_nanos": total,
                "query": q.to_dict(), "start": start, "end": end}


class OptimizationWorker(_Worker):
    name = "optimization"

    def _tools(self):
        return [Tool("candidates", "Recommendations from the active snapshot, at most one per source row.", self._candidates),
                Tool("estimate", "Turn one candidate and its evidence into a finding.", self._estimate),
                Tool("estimate_commitment", "Turn one commitment candidate into a proposal.", self._estimate_commitment),
                Tool("deduplicate", "Count each resource once.", self._deduplicate),
                Tool("sequence_commitments", "Scale proposals down by counted savings in scope.", self._sequence)]

    def _candidates(self) -> tuple[list[dict], bool]:
        out = [{
            "rec_id": r["recommendation_id"], "source": r["source"], "table": "Cost Optimization Hub",
            "account_id": r["account_id"], "region": r["region"], "resource_id": r["resource_id"],
            "resource_arn": r["resource_arn"], "action": r["action_type"], "resource_type": r["current_resource_type"],
            "current": r["current_summary"], "recommended": r["recommended_summary"],
            "details": json.loads(r["recommended_details"]), "savings_nanos": r["savings_after_nanos"],
            "savings_before_nanos": r["savings_before_nanos"], "cost_nanos": r["cost_after_nanos"],
            "cost_before_nanos": r["cost_before_nanos"], "effort": r["effort"], "restart": bool(r["restart_needed"]),
            "rollback": bool(r["rollback_possible"]), "lookback_days": r["lookback_days"], "last_refresh": r["last_refresh"],
            "savings_column": "estimated_monthly_savings_after_discount",
        } for r in self.conn.execute("SELECT * FROM coh_recommendations ORDER BY recommendation_id")]
        seen = {(c["account_id"], c["resource_id"]) for c in out}
        for r in self.conn.execute("SELECT * FROM co_ec2_recommendations WHERE finding='OVER_PROVISIONED' ORDER BY resource_id"):
            if (r["account_id"], r["resource_id"]) in seen:
                continue  # already represented by Cost Optimization Hub: the same advice is not a second finding
            cost = r["current_on_demand_nanos"] * HOURS_PER_MONTH
            out.append({
                "rec_id": "co-" + r["resource_id"], "source": "ComputeOptimizer", "table": "Compute Optimizer",
                "account_id": r["account_id"], "region": r["instance_arn"].split(":")[3], "resource_id": r["resource_id"],
                "resource_arn": r["instance_arn"], "action": "Rightsize", "resource_type": "Ec2Instance",
                "current": r["current_type"], "recommended": r["recommended_type"], "details": {},
                "savings_nanos": r["savings_nanos"], "savings_before_nanos": r["savings_nanos"], "cost_nanos": cost,
                "cost_before_nanos": cost, "effort": r["migration_effort"] or "Medium", "restart": True, "rollback": True,
                "lookback_days": r["lookback_days"], "last_refresh": r["last_refresh"],
                "savings_column": "recommendationOptions_1_estimatedMonthlySavings_value",
            })
        return out[:MAX_CANDIDATES], len(out) > MAX_CANDIDATES

    def _base(self, c: dict, names: dict, window: dict, state: dict) -> dict:
        label = ACTION_LABELS.get(c["action"], c["action"])
        target = c["resource_id"] or c["recommended"]
        return {
            "id": hashlib.sha256(c["rec_id"].encode()).hexdigest()[:16], "recommendation_id": c["rec_id"],
            "title": f"{label}: {target}", "action": c["action"], "action_label": label, "resource_id": c["resource_id"],
            "resource_type": c["resource_type"], "service": SERVICE_BY_TYPE.get(c["resource_type"], c["resource_type"]),
            "account_id": c["account_id"], "account": names.get(c["account_id"], c["account_id"]), "region": c["region"],
            "change": f"{c['current']} → {c['recommended']}", "source": c["table"], "effort": c["effort"],
            "status": state.get("status", "open"), "note": state.get("note", ""),
            "time_range": {"start": window["start"].isoformat(), "end": (window["end"] - timedelta(days=1)).isoformat()},
            "claim_nanos": c["savings_nanos"], "claim_display": _fmt_usd(c["savings_nanos"]), "savings_nanos": None,
            "counted": False, "overlaps": [], "tags": {}, "risks": [], "limitations": list(window["notes"]),
            "realised": REALISED,
        }

    def _estimate(self, cand: dict, facts: dict, util: dict | None, blockers: list[str], names: dict,
                  window: dict, state: dict) -> dict:
        f = self._base(cand, names, window, state)
        team = facts["tags"].get("user:team")
        f.update(owner=team or f["account"],
                 owner_source="user:team tag in CUR" if team else "account (no team tag on this resource)",
                 owner_known=bool(team), tags=facts["tags"], blockers=blockers, days_with_usage=facts["days"],
                 evidence_query=facts["query"] if facts["found"] else None, sp_covered=facts["sp_covered"])
        f["evidence"] = [
            {"label": f"{cand['table']} recommendation", "value": f"{cand['rec_id']}, refreshed {cand['last_refresh'][:10]}, "
                                                                  f"{cand['lookback_days']}-day lookback"},
            {"label": "Source estimate (not verified alone)", "value": f"{_fmt_usd(cand['savings_nanos'])} a month ({cand['savings_column']})"},
        ]
        if facts["found"]:
            f["evidence"].append({"label": "CUR amortized cost", "value": f"{_fmt_usd(facts['amortized_nanos'])} over {facts['days']} days with usage"})
            f["evidence"].append({"label": "CUR charge type", "value": "Savings Plan covered" if facts["sp_covered"] else "On demand"})
        else:
            f["evidence"].append({"label": "CUR cost", "value": "None found in the evidence window"})
        if util:
            memory = f"{util['memory_max']}%" if util["memory_max"] else "not available"
            f["evidence"].append({"label": "Compute Optimizer", "value": f"{util['finding']}, peak CPU {util['cpu_max'] or 'not available'}%, "
                                                                         f"peak memory {memory}, {util['lookback_days']}-day lookback"})
        f["calculation"] = {"formula": "monthly run-rate = amortized cost x 730 / (days with usage x 24)", "steps": []}
        steps = f["calculation"]["steps"]
        if facts["found"]:
            steps.append(f"{_fmt_usd(facts['amortized_nanos'])} x 730 / ({facts['days']} x 24) = {_fmt_usd(facts['monthly_nanos'])} a month")
        if blockers:
            f.update(phase="needs_evidence", monthly_savings_display="Not estimated")
            steps.append(f"No estimate: the source claim of {f['claim_display']} is not supported by the evidence.")
            return f
        steps.append(f"Source estimate {f['claim_display']} is not more than the {_fmt_usd(facts['monthly_nanos'])} run-rate: check passed.")
        if facts["sp_covered"]:
            f.update(phase="conditional", monthly_savings_display=f"Up to {f['claim_display']}")
            steps.append("Not counted: the Savings Plan commitment is still paid after this change.")
            return f
        f.update(phase="plan_ready", savings_nanos=cand["savings_nanos"], monthly_savings_display=f["claim_display"])
        steps.append(f"Estimate = {f['claim_display']} a month.")
        return f

    def _estimate_commitment(self, cand: dict, rate: dict, commitment: dict, names: dict, window: dict, state: dict) -> dict:
        f = self._base(cand, names, window, state)
        baseline = cand["cost_before_nanos"]
        blockers = []
        if cand["savings_nanos"] <= 0 or baseline <= 0:
            blockers.append("The source gives no savings or no on-demand baseline for this commitment.")
        if not rate["found"]:
            blockers.append("No on-demand instance usage in CUR for this account, so there is nothing to commit to.")
        elif abs(baseline - rate["monthly_nanos"]) * 100 > COST_TOLERANCE_PCT * rate["monthly_nanos"]:
            blockers.append(f"The proposal assumes {_fmt_usd(baseline)} of on-demand cost a month, but CUR shows "
                            f"{_fmt_usd(rate['monthly_nanos'])}.")
        if cand["lookback_days"] < MIN_LOOKBACK_DAYS:
            blockers.append(f"The recommendation looks at only {cand['lookback_days']} days. At least {MIN_LOOKBACK_DAYS} are needed.")
        f.update(owner=f["account"], owner_source="account (commitments have no resource tags)", owner_known=False,
                 blockers=blockers, days_with_usage=RATE_DAYS, evidence_query=rate["query"], sp_covered=False,
                 baseline_nanos=baseline, commitment=commitment, hourly=cand["details"].get("hourlyCommitment"),
                 term=cand["details"].get("term", ""), payment=cand["details"].get("paymentOption", ""))
        f["evidence"] = [
            {"label": f"{cand['table']} recommendation", "value": f"{cand['rec_id']} ({cand['source']}), {cand['lookback_days']}-day lookback"},
            {"label": "On-demand baseline assumed by the source", "value": f"{_fmt_usd(baseline)} a month"},
            {"label": f"CUR on-demand instances, last {RATE_DAYS} days", "value": f"{_fmt_usd(rate['monthly_nanos'])} a month"},
            {"label": "Existing Savings Plan, unused share", "value": f"{commitment['unused_pct']}% of commitment" if commitment["unused_pct"] else "No Savings Plan fee in the window"},
        ]
        f["time_range"] = {"start": rate["start"].isoformat(), "end": (rate["end"] - timedelta(days=1)).isoformat()}
        f["calculation"] = {"formula": "proposal = source savings x (baseline - counted savings in scope) / baseline", "steps": []}
        if blockers:
            f.update(phase="needs_evidence", monthly_savings_display="Not estimated")
            f["calculation"]["steps"].append(f"No estimate: the source claim of {f['claim_display']} is not supported by the evidence.")
        else:
            f.update(phase="proposal", monthly_savings_display=f["claim_display"])
        return f

    def _deduplicate(self, findings: list[dict]) -> list[dict]:
        groups: dict[tuple[str, str], list[dict]] = {}
        for f in findings:
            if f["action"] not in COMMITMENT_ACTIONS:
                groups.setdefault((f["account_id"], f["resource_id"]), []).append(f)

        def rank(f: dict) -> tuple:
            effort = EFFORT_ORDER.index(f["effort"]) if f["effort"] in EFFORT_ORDER else len(EFFORT_ORDER)
            return (f["phase"] != "plan_ready", f["phase"] != "conditional", -f["claim_nanos"], effort, f["id"])

        for group in groups.values():
            live = sorted((f for f in group if f["status"] != "dismissed"), key=rank)
            if not live:
                continue
            winner = live[0]
            winner["counted"] = winner["phase"] == "plan_ready"
            for other in live[1:]:
                other["overlaps"] = [winner["id"]]
                if other["phase"] in ("plan_ready", "conditional"):
                    other.update(phase="alternative", savings_nanos=None, counted=False,
                                 monthly_savings_display=f"{other['claim_display']} (alternative)")
                    other["calculation"]["steps"].append(
                        f"Not counted: it competes with “{winner['title']}” on the same resource. Only one can be done.")
        return findings

    def _sequence(self, findings: list[dict]) -> list[dict]:
        for f in findings:
            if f["phase"] != "proposal":
                continue
            in_scope = [x for x in findings if x["counted"] and x["account_id"] == f["account_id"] and x["resource_type"] == "Ec2Instance"]
            removed = sum(x["savings_nanos"] for x in in_scope)
            adjusted = scaled(f["claim_nanos"], f["baseline_nanos"] - removed, f["baseline_nanos"])
            f["removed_nanos"] = removed
            f["adjusted_nanos"] = adjusted
            f["overlaps"] = [x["id"] for x in in_scope]
            f["monthly_savings_display"] = _fmt_usd(adjusted)
            steps = f["calculation"]["steps"]
            steps.append(f"Source savings {f['claim_display']} assume {_fmt_usd(f['baseline_nanos'])} of on-demand cost.")
            steps.append(f"Counted changes in this account remove {_fmt_usd(removed)} ({len(in_scope)} findings).")
            steps.append(f"{f['claim_display']} x ({_fmt_usd(f['baseline_nanos'])} - {_fmt_usd(removed)}) / {_fmt_usd(f['baseline_nanos'])} = {_fmt_usd(adjusted)}")
            steps.append("Shown apart from the counted total. It is never added to it.")
            if f["hourly"]:
                hourly = scaled(parse_usd_to_nanos(f["hourly"]), f["baseline_nanos"] - removed, f["baseline_nanos"])
                f["adjusted_hourly_display"] = _fmt_usd(hourly)
                steps.append(f"Commitment resized the same way: ${f['hourly']}/hour becomes {_fmt_usd(hourly)}/hour.")
        return findings


class RiskWorker(_Worker):
    name = "risk"

    def _tools(self):
        return [Tool("assess", "Risks, limitations and confidence for one finding.", self._assess),
                Tool("assess_commitment", "Risks, limitations and confidence for one proposal.", self._assess_commitment)]

    def _assess(self, f: dict, cand: dict, util: dict | None, snapshot: dict, window: dict) -> dict:
        if f["phase"] == "needs_evidence":
            f.update(confidence="insufficient", confidence_reasons=f["blockers"])
            f["limitations"] = f["blockers"] + f["limitations"]
            return f
        level, reasons = "high", []

        def lower(floor: str, reason: str) -> None:
            nonlocal level
            level = _lowest(level, floor)
            reasons.append(reason)

        risk = Decimal(util["performance_risk"]) if util and util["performance_risk"] else None
        if util and not util["memory_max"]:
            lower("medium", "Memory metric is missing, so only CPU supports this change.")
            f["limitations"].append("Memory use is unknown. A CloudWatch agent is needed to measure it.")
        if f["days_with_usage"] < WINDOW_DAYS:
            lower("medium", f"Only {f['days_with_usage']} days of cost history, not {WINDOW_DAYS}.")
            f["limitations"].append("Short history: a monthly or seasonal peak may not be visible yet.")
        if risk is not None and risk >= 3:
            lower("low", f"Compute Optimizer rates performance risk {util['performance_risk']} of 4 (high).")
            f["risks"].append("High performance risk: the smaller size may not handle peak load.")
        elif risk is not None and risk >= 2:
            lower("medium", f"Compute Optimizer rates performance risk {util['performance_risk']} of 4 (medium).")
        if cand["table"] == "Compute Optimizer":
            lower("medium", "Compute Optimizer prices are on demand, before any discount.")
        monthly = f["evidence_monthly"]
        if abs(cand["cost_nanos"] - monthly) * 100 > COST_TOLERANCE_PCT * monthly:
            lower("low", f"Source cost {_fmt_usd(cand['cost_nanos'])} differs from CUR run-rate {_fmt_usd(monthly)} by more than {COST_TOLERANCE_PCT}%.")
        if snapshot["state"] == "stale":
            lower("low", "The snapshot is old.")
        if f["phase"] == "conditional":
            lower("medium", "The saving depends on other usage absorbing the freed Savings Plan commitment.")
        if not reasons:
            reasons.append("Source, CUR cost and utilisation evidence agree.")
        if cand["restart"]:
            f["risks"].append("Needs a restart: plan a maintenance window.")
        if not cand["rollback"]:
            f["risks"].append("The source says this cannot be rolled back.")
        if not f["owner_known"]:
            f["risks"].append("No team tag: confirm who owns this before any change.")
        if f["phase"] == "conditional":
            f["risks"].append("This instance is covered by a Savings Plan. The commitment does not shrink, so this saving "
                              "is real only if other eligible usage absorbs the freed commitment.")
            f["limitations"].append("The saving is shown as “up to” and is not counted.")
        if cand["action"] == "MigrateToGraviton":
            f["risks"].append("Graviton is a different CPU architecture: the software must support ARM.")
        if cand["action"] == "Stop":
            f["risks"].append("Anything that depends on this instance stops working.")
        if cand["resource_type"] == "EbsVolume":
            f["risks"].append("Performance limits change with the volume type: check IOPS and throughput first.")
        f["limitations"].append("This release uses CUR and exports only. It cannot see the live state of the resource.")
        f.update(confidence=level, confidence_reasons=reasons)
        return f

    def _assess_commitment(self, f: dict, snapshot: dict) -> dict:
        if f["phase"] == "needs_evidence":
            f.update(confidence="insufficient", confidence_reasons=f["blockers"])
            f["limitations"] = f["blockers"] + f["limitations"]
            return f
        level, reasons = "medium", ["A commitment depends on future usage, which cannot be known."]
        unused = f["commitment"]["unused_nanos"]
        f["risks"] += ["You pay the commitment for the full term, even if usage drops.",
                       "A Savings Plan cannot be cancelled after the return window."]
        if unused > 0:
            level = "low"
            reasons.append(f"The existing Savings Plan already has {f['commitment']['unused_pct']}% unused commitment.")
            f["risks"].append("Use up the existing unused commitment before buying more.")
        if f.get("removed_nanos"):
            f["risks"].append("Buying before the changes above are done would over-commit.")
        if snapshot["state"] == "stale":
            level = "low"
            reasons.append("The snapshot is old.")
        f["limitations"].append("Scaling is proportional. A real plan needs the usage measured again after the changes.")
        f.update(confidence=level, confidence_reasons=reasons)
        return f


class ActionPlanningWorker(_Worker):
    name = "action_planning"

    def _tools(self):
        return [Tool("plan", "A reviewable plan. It describes work; it performs none.", self._plan)]

    def _plan(self, f: dict, cand: dict) -> dict | None:
        if f["phase"] == "needs_evidence":
            f["action_plan"] = None
            f["next_step"] = "Get the missing evidence listed above. No plan is written until then."
            return f
        benefit = f"Estimated {f['monthly_savings_display']} a month. This is an estimate, not a realised saving."
        base = {"kind": "change", "owner": f["owner"], "expected_benefit": benefit, "risks": f["risks"],
                "execution": "CostLens does not run this. A person must review and carry it out."}
        if cand["action"] in COMMITMENT_ACTIONS:
            size = f.get("adjusted_hourly_display", "the resized amount")
            base.update(kind="commitment_proposal", prerequisites=[
                "Finish or reject the counted changes in this account first.",
                "Measure on-demand usage again after those changes.",
                "Get finance approval for the term and payment option."],
                validation=[f"Recalculate the commitment. It should be near {size}/hour, not the source figure.",
                            "After purchase, check utilisation stays near 100% for 30 days."],
                rollback=["No rollback after the return window. Size the commitment on the low side."],
                execution="Proposal only. CostLens never buys a commitment.")
        elif cand["action"] == "Stop":
            base.update(prerequisites=["Confirm the owner.", "Confirm nothing depends on this instance.", "Take a snapshot or AMI if the data matters."],
                        validation=["Check no errors or alarms for 7 days.", "Check the instance cost goes to zero in CUR."],
                        rollback=["Start the instance again. Attached EBS volumes keep charging while it is stopped."])
        elif cand["resource_type"] == "EbsVolume":
            base.update(prerequisites=["Confirm the owner.", "Confirm gp3 baseline IOPS and throughput meet the workload."],
                        validation=["Compare volume latency for 7 days before and after.", "Check the per-GB rate in CUR drops."],
                        rollback=["Modify the volume back. EBS allows one change per volume every 6 hours."])
        else:
            base.update(prerequisites=["Confirm the owner.", f"Test {cand['recommended']} in a lower environment.",
                                       "Check peak load, including month-end jobs."],
                        validation=["Compare CPU, memory and latency for 7 days before and after.",
                                    f"Check {f['resource_id']} shows the new usage type in CUR."],
                        rollback=[f"Change the instance back to {cand['current']}. Needs a restart."] if cand["rollback"]
                        else ["The source says this cannot be rolled back. Review before going ahead."])
        if f["phase"] == "conditional":
            base["prerequisites"] = ["Show that other eligible usage will absorb the freed Savings Plan commitment."] + base["prerequisites"]
        f["action_plan"] = base
        f["next_step"] = "Review the plan with the owner."
        return f


class OptimizationPipeline:
    def __init__(self, conn):
        self.conn = conn
        self.trace: dict[str, int] = {}
        self.quality = DataQualityWorker(conn, self.trace)
        self.investigation = InvestigationWorker(conn, self.trace)
        self.optimization = OptimizationWorker(conn, self.trace)
        self.risk = RiskWorker(conn, self.trace)
        self.planning = ActionPlanningWorker(conn, self.trace)

    def run(self) -> dict:
        snapshot = self.quality.call("snapshot_status")
        window = self.quality.call("evidence_window")
        if snapshot["state"] == "no_data" or window is None:
            reason = ("No Cost Optimization Hub or Compute Optimizer snapshot has been ingested."
                      if snapshot["state"] == "no_data" else "No CUR data has been ingested, so nothing can be verified.")
            AuditLog(self.conn).record("optimize.run", {"state": "no_data", "tools": self.trace})
            return {"state": "no_data", "reason": reason, "snapshot": snapshot, "opportunities": [],
                    "totals": _totals([]), "realised": REALISED, "workers": self.trace}
        names = account_names(self.conn)
        states = {r["finding_id"]: dict(r) for r in self.conn.execute("SELECT * FROM opt_states")}
        commitment = self.investigation.call("commitment_usage", window=window)
        candidates, truncated = self.optimization.call("candidates")
        if truncated:
            snapshot["notes"].append(f"Only the first {MAX_CANDIDATES} recommendations were checked.")

        findings, sources = [], {}
        for c in candidates:
            state = states.get(hashlib.sha256(c["rec_id"].encode()).hexdigest()[:16], {})
            if c["action"] in COMMITMENT_ACTIONS:
                rate = self.investigation.call("on_demand_rate", account_id=c["account_id"], window=window)
                f = self.optimization.call("estimate_commitment", cand=c, rate=rate, commitment=commitment,
                                           names=names, window=window, state=state)
            else:
                facts = self.investigation.call("resource_facts", account_id=c["account_id"], resource_id=c["resource_id"], window=window) \
                    if c["resource_id"] else {"found": False, "days": 0, "monthly_nanos": 0, "amortized_nanos": 0, "tags": {},
                                              "sp_covered": False, "unsupported_rows": 0, "query": None}
                util = self.investigation.call("utilisation", account_id=c["account_id"], resource_id=c["resource_id"])
                blockers = self.quality.call("check_evidence", cand=c, facts=facts, util=util)
                f = self.optimization.call("estimate", cand=c, facts=facts, util=util, blockers=blockers,
                                           names=names, window=window, state=state)
                f["evidence_monthly"] = facts["monthly_nanos"]
                self.risk.call("assess", f=f, cand=c, util=util, snapshot=snapshot, window=window)
            sources[f["id"]] = c
            findings.append(f)

        self.optimization.call("deduplicate", findings=findings)
        self.optimization.call("sequence_commitments", findings=findings)
        for f in findings:
            if f["action"] in COMMITMENT_ACTIONS:
                self.risk.call("assess_commitment", f=f, snapshot=snapshot)
            self.planning.call("plan", f=f, cand=sources[f["id"]])
            f["phase_label"] = PHASES[f["phase"]]
        order = {"plan_ready": 0, "conditional": 1, "proposal": 2, "alternative": 3, "needs_evidence": 4}
        findings.sort(key=lambda f: (f["status"] == "dismissed", order[f["phase"]], -f["claim_nanos"], f["id"]))
        AuditLog(self.conn).record("optimize.run", {"state": "ok", "findings": len(findings), "tools": self.trace})
        return {
            "state": "ok", "snapshot": snapshot, "opportunities": findings, "totals": _totals(findings),
            "realised": REALISED, "workers": self.trace, "reference_date": engine.reference_date(self.conn).isoformat(),
            "window": {"start": window["start"].isoformat(), "end": (window["end"] - timedelta(days=1)).isoformat(),
                       "days": window["days"], "notes": window["notes"]},
            "data_quality": _quality_report(findings, snapshot, window),
            "method": {
                "Monthly run-rate": "Amortized cost x 730 / (days with usage x 24), over the last 30 days of CUR",
                "Counted": "Only findings with enough evidence. One resource counts once.",
                "Overlaps": "Competing recommendations on one resource are alternatives. Only the best supported one is counted.",
                "Savings Plan conflict": "Rightsizing a covered instance is conditional and not counted: the commitment is still paid.",
                "Commitments": "Proposal only, scaled down by counted savings in scope, and never added to the total.",
            },
        }


def _quality_report(findings: list[dict], snapshot: dict, window: dict) -> list[dict]:
    resources = [f for f in findings if f["action"] not in COMMITMENT_ACTIONS]
    matched = sum(1 for f in resources if f["evidence_query"])
    blocked = sum(1 for f in findings if f["phase"] == "needs_evidence")
    return [
        {"check": "Snapshot age", "state": "ok" if snapshot["state"] == "ok" else "warn",
         "detail": f"{snapshot['execution_id']}, {snapshot['age_days']} days old (limit {MAX_SNAPSHOT_AGE_DAYS})"},
        {"check": "Matched to CUR", "state": "ok" if matched == len(resources) else "warn",
         "detail": f"{matched} of {len(resources)} resource recommendations have cost in CUR"},
        {"check": "Enough evidence", "state": "ok" if not blocked else "warn",
         "detail": f"{len(findings) - blocked} of {len(findings)} findings can be estimated"},
        {"check": "Billing data", "state": "ok" if not window["notes"] else "warn",
         "detail": "; ".join(window["notes"]) or "All periods in the window are final"},
    ]


def _totals(findings: list[dict]) -> dict:
    live = [f for f in findings if f["status"] != "dismissed"]
    counted = [f for f in live if f["counted"]]
    ids = [(f["account_id"], f["resource_id"]) for f in counted]
    assert len(ids) == len(set(ids)), "a resource must never be counted twice"
    amounts = {
        "counted": sum(f["savings_nanos"] for f in counted),
        "conditional": sum(f["claim_nanos"] for f in live if f["phase"] == "conditional"),
        "proposal": sum(f.get("adjusted_nanos", 0) for f in live if f["phase"] == "proposal"),
        "alternative": sum(f["claim_nanos"] for f in live if f["phase"] == "alternative"),
        "unverified": sum(f["claim_nanos"] for f in live if f["phase"] == "needs_evidence"),
        "raw": sum(f["claim_nanos"] for f in findings),
    }
    out = {k + "_nanos": v for k, v in amounts.items()}
    out.update({k + "_display": _fmt_usd(v) for k, v in amounts.items()})
    out["counted_count"] = len(counted)
    out["needs_evidence_count"] = sum(1 for f in live if f["phase"] == "needs_evidence")
    return out


def _matches(f: dict, filters: dict[str, list[str]]) -> bool:
    for dim, values in filters.items():
        if dim == "service":
            services = {f["service"]} | ({"AmazonEC2"} if f["service"] == "ComputeSavingsPlans" else set())
            if not services & set(values):
                return False
        elif dim == "account" and f["account_id"] not in values:
            return False
        elif dim == "region" and f["region"] not in values:
            return False
        elif dim.startswith("tag:") and f["tags"].get(dim[4:]) not in values:
            return False
    return True


def summary(conn, filters: dict[str, list[str]] | None = None) -> dict:
    result = OptimizationPipeline(conn).run()
    if filters:
        result["opportunities"] = [f for f in result["opportunities"] if _matches(f, filters)]
        result["totals"] = _totals(result["opportunities"])
    return result


def set_status(conn, finding_id: str, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute(
        "INSERT INTO opt_states(finding_id, status, note, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(finding_id) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at",
        (finding_id, status, note[:500], utc_now()))


def answer(conn, understood: list[str], filters: dict[str, list[str]], realised_question: bool) -> dict:
    """The chat reply. Every number comes from the same pipeline as the Optimize page."""
    s = summary(conn, filters)
    reply = {"kind": "optimize", "worker": "optimize", "understood": understood, "totals": s["totals"],
             "opportunities": s["opportunities"][:5], "total": len(s["opportunities"]), "state": s["state"]}
    if s["state"] != "ok":
        reply["message"] = f"{s['reason']} I cannot suggest savings without evidence, so I will not give a number."
        return reply
    t = s["totals"]
    scope = f" for {', '.join(understood)}" if understood else ""
    if realised_question:
        reply["message"] = (f"Realised savings: not measured. {REALISED['detail']} Estimated savings still open{scope}: "
                            f"{t['counted_display']} a month.")
    elif not s["opportunities"]:
        reply["message"] = (f"I have no recommendations{scope} in the current snapshot, so I have no finding and no "
                            "savings number for this. That does not prove there is nothing to save.")
    else:
        parts = [f"I found {t['counted_count']} opportunit{'ies' if t['counted_count'] != 1 else 'y'}{scope} with enough "
                 f"evidence: an estimated {t['counted_display']} a month, with no overlaps."]
        if t["conditional_nanos"]:
            parts.append(f"Up to {t['conditional_display']} more depends on the Savings Plan, so it is not counted.")
        if t["proposal_nanos"]:
            parts.append(f"A commitment proposal of about {t['proposal_display']} a month is shown apart and is not added.")
        if t["needs_evidence_count"]:
            parts.append(f"{t['needs_evidence_count']} more need{'s' if t['needs_evidence_count'] == 1 else ''} evidence, so I give no number for them.")
        parts.append("These are estimates. Nothing has been changed.")
        reply["message"] = " ".join(parts)
    reply["suggestions"] = ["How can I reduce EC2 cost?", "How much can prod-data save?", "How can I reduce RDS cost?"]
    return reply
