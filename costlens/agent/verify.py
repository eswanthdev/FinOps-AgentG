"""Savings verification: change record -> baseline -> attribution -> verification.

Deterministic, read-only, local. A person records that a change was made; CostLens
measures what CUR shows afterwards. It never makes the change, and "implemented" is
self-reported until CUR agrees. Results are computed from CUR on every run and never
stored, so a billing revision updates them.

Method (documented so results are reproducible). All money is integer nano-USD:
  * Baseline: up to 14 days before the change, starting no earlier than a previous
    change on the same resource. Measured period: from the change to the next change
    on that resource, using final billing when that gives at least 7 days.
  * expected_no_change = baseline cost x after days / baseline days
  * expected_old_rate  = baseline cost x after usage / baseline usage
  * usage effect = expected_no_change - expected_old_rate
  * rate effect  = expected_old_rate - actual
  * Rightsize, upgrade and migrate get credit for the rate effect only: lower usage
    is not a saving from the change. Stop and delete get both effects.
  * Commitment effect: growth in unused Savings Plan commitment after a change to a
    covered resource is taken off. It is shared out by date, so it is deducted once.
  * Credits and refunds are shown apart and are not counted.
"""
from __future__ import annotations

from datetime import date, timedelta
from fractions import Fraction

from ..money import share_pct
from ..query import engine
from ..query.engine import _fmt_usd
from ..query.model import CostQuery
from ..store import utc_now
from . import optimize
from .optimize import HOURS_PER_MONTH, _Worker
from .orchestrator import AuditLog, Tool

BASELINE_DAYS = 14
MIN_AFTER_DAYS = 7
FULL_PCT = 80          # at least this share of the estimate counts as fully realised
NOT_REALISED_PCT = 5   # below this share, nothing was realised
COST_TYPES = ["Usage", "SavingsPlanCoveredUsage", "SavingsPlanNegation"]
USAGE_ROWS = ("Usage", "SavingsPlanCoveredUsage")
CREDIT_TYPES = ["Credit", "Refund"]
MARKABLE_PHASES = ("plan_ready", "conditional", "alternative")
FULL_CREDIT_ACTIONS = ("Stop", "Delete")
STATES = {"waiting": "Waiting", "provisional": "Provisional", "verified": "Verified", "partly_realised": "Partly realised",
          "not_realised": "Not realised", "cannot_verify": "Cannot verify"}


class ChangeRecordWorker(_Worker):
    name = "change_record"

    def _tools(self):
        return [Tool("changes", "Recorded changes, oldest first.", self._changes)]

    def _changes(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM opt_changes ORDER BY implemented_on, finding_id")]


class BaselineWorker(_Worker):
    name = "baseline"

    def _tools(self):
        return [Tool("after_window", "The measured period for one change.", self._after),
                Tool("measure", "Cost and usage of one resource in one window.", self._measure),
                Tool("unused_commitment", "Unused Savings Plan commitment in one window.", self._unused)]

    def _after(self, start: date, limit: date) -> dict:
        """limit is exclusive: the next change on this resource, or the day after the data ends."""
        last = self.conn.execute("SELECT MAX(usage_date) AS d FROM line_items").fetchone()["d"]
        end = min(limit, date.fromisoformat(last) + timedelta(days=1)) if last else start
        if end <= start:
            return {"end": start, "days": 0, "final": False}
        final_end = start
        for p in engine.coverage(self.conn, start, end)["periods"]:
            if p.get("status") != "final":
                break
            final_end = min(end, date.fromisoformat(p["data_through"]) + timedelta(days=1))
        if (final_end - start).days >= MIN_AFTER_DAYS:
            return {"end": final_end, "days": (final_end - start).days, "final": True}
        return {"end": end, "days": (end - start).days, "final": False}

    def _measure(self, account_id: str, resource_id: str, start: date, end: date) -> dict:
        q = CostQuery(start=start, end=end, metric="amortized", granularity="daily", group_by="usage_type",
                      filters={"account": [account_id], "resource": [resource_id], "line_item_type": COST_TYPES},
                      chart="stacked").validate()
        where, params = engine._where(q, start, end)
        rows = self.conn.execute(f"SELECT usage_date, usage_type, usage_amount, line_item_type FROM line_items WHERE {where}", params).fetchall()
        usage = sum((Fraction(r["usage_amount"]) for r in rows if r["line_item_type"] in USAGE_ROWS), Fraction(0))
        credits = CostQuery(start=start, end=end, metric="unblended", granularity="total", group_by=None,
                            filters={"account": [account_id], "resource": [resource_id], "line_item_type": CREDIT_TYPES}).validate()
        return {"cost_nanos": engine._window_total(self.conn, q, start, end), "usage": usage,
                "days_with_usage": len({r["usage_date"] for r in rows if r["line_item_type"] in USAGE_ROWS}),
                "usage_types": sorted({r["usage_type"] for r in rows if r["line_item_type"] in USAGE_ROWS}),
                "covered": any(r["line_item_type"] == "SavingsPlanCoveredUsage" for r in rows),
                "unsupported": engine._unsupported_amortized(self.conn, where, params),
                "credit_nanos": engine._window_total(self.conn, credits, start, end), "query": q.to_dict()}

    def _unused(self, start: date, end: date) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(amortized_nanos),0) AS t FROM line_items WHERE line_item_type='SavingsPlanRecurringFee' "
            "AND usage_date >= ? AND usage_date < ?", (start.isoformat(), end.isoformat())).fetchone()["t"]


class AttributionWorker(_Worker):
    name = "attribution"

    def _tools(self):
        return [Tool("split", "Split a cost change into usage and rate effects.", self._split),
                Tool("commitment_effect", "Growth in unused commitment not yet claimed by an earlier change.", self._commitment)]

    def _split(self, base: dict, after: dict, base_days: int, after_days: int) -> dict:
        no_change = base["cost_nanos"] * after_days // base_days
        old_rate = int(Fraction(base["cost_nanos"]) * after["usage"] / base["usage"]) if base["usage"] else 0
        actual = after["cost_nanos"]
        return {"expected_no_change": no_change, "expected_old_rate": old_rate, "actual": actual,
                "usage_effect": no_change - old_rate, "rate_effect": old_rate - actual, "total_change": no_change - actual}

    def _commitment(self, unused_base: int, unused_after: int, base_days: int, after_days: int, claimed_daily: int) -> dict:
        growth_daily = max(0, unused_after // after_days - unused_base // base_days)
        free_daily = max(0, growth_daily - claimed_daily)
        return {"daily": free_daily, "total": free_daily * after_days}


class VerificationWorker(_Worker):
    name = "verification"

    def _tools(self):
        return [Tool("judge", "The result state for one measured change.", self._judge)]

    def _judge(self, estimate: int, monthly: int, expected_seen: bool, final: bool) -> str:
        if monthly * 100 < NOT_REALISED_PCT * estimate or monthly <= 0:
            outcome = "not_realised"
        elif monthly * 100 < FULL_PCT * estimate or not expected_seen:
            outcome = "partly_realised"
        else:
            outcome = "verified"
        return outcome if final else "provisional"


class VerificationPipeline:
    def __init__(self, conn):
        self.conn = conn
        self.trace: dict[str, int] = {}
        self.records = ChangeRecordWorker(conn, self.trace)
        self.baseline = BaselineWorker(conn, self.trace)
        self.attribution = AttributionWorker(conn, self.trace)
        self.verification = VerificationWorker(conn, self.trace)

    def _result(self, c: dict, state: str, reason: str, **extra) -> dict:
        return {"finding_id": c["finding_id"], "recommendation_id": c["recommendation_id"], "title": c["title"], "account": c["account"],
                "account_id": c["account_id"], "resource_id": c["resource_id"], "action": c["action"], "implemented_on": c["implemented_on"],
                "note": c["note"], "state": state, "state_label": STATES[state], "reason": reason, "estimate_nanos": c["estimate_nanos"],
                "estimate_display": _fmt_usd(c["estimate_nanos"]), "realised_nanos": None, "monthly_nanos": None,
                "realised_display": "No number", "monthly_display": "No number", "split": [], "steps": [],
                "limitations": ["“Implemented” is self-reported. There is no login, so it is not verified."], **extra}

    def _one(self, c: dict, previous: date | None, next_change: date | None, claimed: dict) -> dict:
        day = date.fromisoformat(c["implemented_on"])
        base_start = max(day - timedelta(days=BASELINE_DAYS), previous or date.min)
        base_days = (day - base_start).days
        limit = next_change or date.max
        window = self.baseline.call("after_window", start=day, limit=limit)
        if window["days"] < MIN_AFTER_DAYS:
            return self._result(c, "waiting", f"Only {window['days']} days of billing after the change. At least {MIN_AFTER_DAYS} are needed.")
        end = window["end"]
        periods = engine.coverage(self.conn, base_start, end)["periods"]
        missing = [p["period"] for p in periods if p.get("status") == "missing"]
        if missing:
            return self._result(c, "cannot_verify", f"No billing data for {', '.join(missing)}, so before and after cannot be compared.")
        base = self.baseline.call("measure", account_id=c["account_id"], resource_id=c["resource_id"], start=base_start, end=day)
        after = self.baseline.call("measure", account_id=c["account_id"], resource_id=c["resource_id"], start=day, end=end)
        if base["unsupported"] or after["unsupported"]:
            return self._result(c, "cannot_verify", "Some charges cannot be amortized in this release, so the true cost is unknown.")
        needed = min(BASELINE_DAYS, max(base_days, MIN_AFTER_DAYS))  # a later change on a resource may have a shorter baseline
        if base["days_with_usage"] < needed or not base["usage"]:
            return self._result(c, "cannot_verify", f"The baseline has only {base['days_with_usage']} days of usage for this resource.")
        full_credit = c["action"] in FULL_CREDIT_ACTIONS
        if not after["usage"] and not full_credit:
            return self._result(c, "cannot_verify", "This resource has no usage after the change. It may have been replaced or stopped, "
                                                    "so its cost cannot be followed.")
        split = self.attribution.call("split", base=base, after=after, base_days=base_days, after_days=window["days"])
        commitment = {"daily": 0, "total": 0}
        if base["covered"]:
            commitment = self.attribution.call(
                "commitment_effect", unused_base=self.baseline.call("unused_commitment", start=base_start, end=day),
                unused_after=self.baseline.call("unused_commitment", start=day, end=end), base_days=base_days,
                after_days=window["days"], claimed_daily=claimed["daily"])
        credited = split["total_change"] if full_credit else split["rate_effect"]
        deduction = min(commitment["total"], max(0, credited))
        claimed["daily"] += deduction // window["days"]
        realised = credited - deduction
        monthly = realised * HOURS_PER_MONTH // (window["days"] * 24)
        token = c["recommended"].split(",")[0].split(" ")[0]
        expected_seen = full_credit or any(token in t for t in after["usage_types"])
        state = self.verification.call("judge", estimate=c["estimate_nanos"], monthly=monthly, expected_seen=expected_seen, final=window["final"])
        reasons = {
            "verified": "CUR shows the expected change, and the saving is close to the estimate.",
            "partly_realised": "The measured saving is well below the estimate, or the expected change does not show in CUR.",
            "not_realised": "CUR shows no saving from this change.",
            "provisional": "Measured on billing that is not final, so this may change.",
        }
        r = self._result(c, state, reasons[state])
        if commitment["total"] and deduction:
            r["reason"] += " The rate fell, but the freed commitment went unused, so the bill did not."
        elif not full_credit and split["usage_effect"] > 0 and realised <= 0:
            r["reason"] += " Cost fell because usage fell, and that is not a saving from this change."
        fmt = _fmt_usd
        r.update(realised_nanos=realised, monthly_nanos=monthly, realised_display=fmt(realised), monthly_display=fmt(monthly),
                 share_of_estimate=share_pct(monthly, c["estimate_nanos"]), expected_seen=expected_seen,
                 before_query=base["query"], after_query=after["query"], final=window["final"],
                 baseline_window={"start": base_start.isoformat(), "end": (day - timedelta(days=1)).isoformat(), "days": base_days},
                 after_window={"start": day.isoformat(), "end": (end - timedelta(days=1)).isoformat(), "days": window["days"]},
                 split=[{"label": "Rate effect", "value": fmt(split["rate_effect"]), "counted": True},
                        {"label": "Usage effect", "value": fmt(split["usage_effect"]), "counted": full_credit},
                        {"label": "Commitment effect", "value": fmt(-deduction), "counted": True},
                        {"label": "Credits and refunds", "value": fmt(after["credit_nanos"]), "counted": False}],
                 steps=[f"Baseline {fmt(base['cost_nanos'])} over {base_days} days. After: {fmt(split['actual'])} over {window['days']} days.",
                        f"Without the change: {fmt(base['cost_nanos'])} x {window['days']} / {base_days} = {fmt(split['expected_no_change'])}",
                        f"At the old rate with actual usage: {fmt(split['expected_old_rate'])}",
                        f"Credited: {fmt(credited)} ({'all of the change' if full_credit else 'rate effect only'}), less {fmt(deduction)} commitment effect = {fmt(realised)}",
                        f"Monthly rate: {fmt(realised)} x 730 / ({window['days']} x 24) = {fmt(monthly)}"])
        r["limitations"].append("Other causes, such as a price change or work moving elsewhere, cannot all be ruled out.")
        if not expected_seen:
            r["limitations"].append(f"The expected {token} does not show in CUR after the change. Found: {', '.join(after['usage_types'])}.")
        if commitment["total"]:
            r["limitations"].append("All growth in unused commitment after this change is treated as caused by it. That is cautious.")
        return r

    def run(self) -> dict:
        changes = self.records.call("changes")
        by_resource: dict[tuple[str, str], list[dict]] = {}
        for c in changes:
            by_resource.setdefault((c["account_id"], c["resource_id"]), []).append(c)
        claimed = {"daily": 0}
        results = []
        for c in changes:
            group = by_resource[(c["account_id"], c["resource_id"])]
            i = group.index(c)
            previous = date.fromisoformat(group[i - 1]["implemented_on"]) if i else None
            following = date.fromisoformat(group[i + 1]["implemented_on"]) if i + 1 < len(group) else None
            results.append(self._one(c, previous, following, claimed))
        if changes:
            AuditLog(self.conn).record("verify.run", {"changes": len(changes), "tools": self.trace})
        return {"results": results, "totals": _totals(results), "workers": self.trace, "method": {
            "Baseline": f"Up to {BASELINE_DAYS} days before the change, and never before a previous change on the same resource",
            "Measured period": f"From the change to the next change, on final billing when that gives at least {MIN_AFTER_DAYS} days",
            "Credit": "Rightsize and upgrade get the rate effect only, so usage changes do not count. Stop and delete get the full drop.",
            "Commitment": "Growth in unused Savings Plan commitment is taken off, once.",
            "Stored": "Nothing. Results are recalculated from CUR, so a billing revision updates them.",
        }}


def _totals(results: list[dict]) -> dict:
    final = [r for r in results if r["state"] in ("verified", "partly_realised", "not_realised")]
    provisional = [r for r in results if r["state"] == "provisional"]
    final_nanos = sum(r["realised_nanos"] for r in final)
    provisional_nanos = sum(r["realised_nanos"] for r in provisional)
    counts = {k: sum(1 for r in results if r["state"] == k) for k in STATES}
    return {"final_nanos": final_nanos, "final_display": _fmt_usd(final_nanos), "provisional_nanos": provisional_nanos,
            "provisional_display": _fmt_usd(provisional_nanos), "counts": counts, "changes": len(results)}


def attach(conn, summary: dict) -> dict:
    """Add verification to an Optimize summary, keeping estimates and realised numbers apart."""
    v = VerificationPipeline(conn).run()
    summary["verification"] = v
    if not v["results"]:
        return summary
    t = v["totals"]
    detail = (f"{t['final_display']} measured on final billing, {t['provisional_display']} provisional, "
              f"from {t['changes']} recorded changes. These are measured separately from estimates.")
    summary["realised"] = {"state": "measured", "display": t["final_display"], "detail": detail}
    summary["totals"]["realised_display"] = t["final_display"]
    summary["totals"]["realised_sub"] = f"final billing, {t['provisional_display']} provisional"
    results = {r["finding_id"]: r for r in v["results"]}
    for f in summary["opportunities"]:
        r = results.get(f["id"])
        if r:
            f["realised"] = {"state": r["state"], "display": f"{r['state_label']}, {r['realised_display']} so far", "detail": r["reason"]}
    return summary


def mark_implemented(conn, finding_id: str, implemented_on: str, note: str = "") -> dict:
    try:
        day = date.fromisoformat(implemented_on)
    except ValueError as exc:
        raise ValueError("implemented date must be YYYY-MM-DD") from exc
    if day > engine.reference_date(conn):
        raise ValueError("implemented date cannot be in the future")
    if conn.execute("SELECT 1 FROM opt_changes WHERE finding_id=?", (finding_id,)).fetchone():
        raise ValueError("this finding is already marked as implemented")
    f = next((x for x in optimize.OptimizationPipeline(conn).run()["opportunities"] if x["id"] == finding_id), None)
    if f is None:
        raise ValueError("finding not found")
    if f["phase"] not in MARKABLE_PHASES:
        raise ValueError(f"a finding in phase {f['phase_label']} cannot be measured in this release")
    if conn.execute("SELECT 1 FROM opt_changes WHERE account_id=? AND resource_id=? AND implemented_on=?",
                    (f["account_id"], f["resource_id"], day.isoformat())).fetchone():
        raise ValueError("another change on this resource has the same date, so they could not be told apart")
    conn.execute(
        "INSERT INTO opt_changes(finding_id, recommendation_id, title, account_id, account, resource_id, action, recommended, "
        "estimate_nanos, implemented_on, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f["id"], f["recommendation_id"], f["title"], f["account_id"], f["account"], f["resource_id"], f["action"],
         f["change"].split(" → ")[-1], f["claim_nanos"], day.isoformat(), note[:500], utc_now()))
    return {"id": finding_id, "implemented_on": day.isoformat()}


def undo(conn, finding_id: str) -> bool:
    return conn.execute("DELETE FROM opt_changes WHERE finding_id=?", (finding_id,)).rowcount > 0


def load_savings_scenario(conn, root) -> list:
    """SYNTHETIC demo: record the changes on the "before" data, then let the later billing arrive."""
    from .. import fixtures
    from ..ingest.loader import ingest_source
    from ..ingest.sources import LocalDirectorySource
    from ..store import set_meta
    set_meta(conn, "reference_date", fixtures.SAVINGS_REFERENCE_DATE.isoformat())
    if not conn.execute("SELECT 1 FROM opt_changes LIMIT 1").fetchone():
        ids = {f["recommendation_id"]: f["id"] for f in optimize.OptimizationPipeline(conn).run()["opportunities"]}
        for c in fixtures.savings_changes():
            mark_implemented(conn, ids[c["recommendation_id"]], c["implemented_on"], c["note"])
    return ingest_source(conn, LocalDirectorySource(fixtures.generate_savings_after(root)))
