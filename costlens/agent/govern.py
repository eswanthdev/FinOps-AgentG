"""Governance: policy check -> impact simulation -> risk -> guardrail drafting.

Deterministic, read-only, local. CostLens drafts guardrails; it never applies them.
"Approved" records a decision in this app. There is no login yet, so it proves nothing
about who decided, and it changes nothing in AWS.

Rules (documented so results are reproducible):
  * Evidence window: the last 30 days of ingested CUR, amortized cost.
  * One violation per policy and account. Spend is what the policy touched in the window.
  * The spend total counts each resource once, even if it breaks several policies.
    Budget coverage is left out: it describes a whole account, not extra spend.
  * An exception hides a violation until its expiry date, then it opens again.
  * The impact of a draft is an estimate from billing data. CUR shows what was billed,
    not API calls, and a guardrail blocks new actions: it does not stop running resources.
  * An approved draft whose content changes goes back to draft.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta

from ..ingest.governance import PolicySpec
from ..query import engine
from ..query.engine import _fmt_usd, account_names
from ..query.model import CostQuery
from ..store import get_meta, utc_now
from . import optimize
from .optimize import _Worker
from .orchestrator import AuditLog, Tool

WINDOW_DAYS = 30
MAX_RESOURCES = 200
MAX_EXCEPTION_DAYS = 365
SCP_MAX_BYTES = 5120
TAG_POLICY_MAX_BYTES = 2500
MAX_SCPS_PER_TARGET = 5
USAGE_TYPES = ["Usage", "SavingsPlanCoveredUsage"]
GLOBAL_SERVICES = ["budgets:*", "ce:*", "cloudfront:*", "iam:*", "organizations:*", "route53:*", "sts:*", "support:*"]
TRANSITIONS = {"draft": ("in_review",), "in_review": ("approved", "rejected", "draft"),
               "approved": ("draft",), "rejected": ("draft",)}
STATUS_LABELS = {"draft": "Draft", "in_review": "In review", "approved": "Approved (recorded, not verified)",
                 "rejected": "Rejected"}
NOT_APPLIED = "CostLens does not apply this. A person must review it and apply it in AWS."


def check_document(document: dict, kind: str) -> list[dict]:
    """Format and size checks. These do not prove a document works in AWS."""
    text = json.dumps(document, separators=(",", ":"))
    limit = SCP_MAX_BYTES if kind == "SCP" else TAG_POLICY_MAX_BYTES
    size = len(text.encode())
    checks = [{"check": "Size", "state": "ok" if size <= limit else "fail", "detail": f"{size} of {limit} bytes"}]
    if kind == "SCP":
        statements = document.get("Statement")
        good = (document.get("Version") == "2012-10-17" and isinstance(statements, list) and bool(statements)
                and all(s.get("Effect") == "Deny" and ("Action" in s or "NotAction" in s) and "Resource" in s for s in statements))
        checks.append({"check": "Format", "state": "ok" if good else "fail",
                       "detail": "Version 2012-10-17, Deny statements with an action and a resource" if good
                       else "Not a valid deny-only SCP"})
    else:
        tags = document.get("tags")
        good = isinstance(tags, dict) and bool(tags) and all("tag_key" in t for t in tags.values())
        checks.append({"check": "Format", "state": "ok" if good else "fail",
                       "detail": "Tag policy with a tag_key" if good else "Not a valid tag policy"})
    return checks


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class PolicyCheckWorker(_Worker):
    name = "policy_check"

    def _tools(self):
        return [Tool("policies", "Enabled and disabled policies.", self._policies),
                Tool("evaluate", "Violations of one policy in the evidence window.", self._evaluate)]

    def _policies(self) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM gov_policies ORDER BY id"):
            spec = PolicySpec.from_dict({"id": r["id"], "name": r["name"], "kind": r["kind"], "params": json.loads(r["params_json"])})
            out.append({"id": spec.id, "name": spec.name, "kind": spec.kind, "params": spec.params, "enabled": bool(r["enabled"])})
        return out

    def _query(self, window: dict, filters: dict) -> CostQuery:
        return CostQuery(start=window["start"], end=window["end"], metric="amortized", granularity="daily",
                         group_by="resource", filters=filters, chart="stacked").validate()

    def _measure(self, q: CostQuery) -> dict:
        where, params = engine._where(q, q.start, q.end)
        rows = self.conn.execute(
            "SELECT resource_id, SUM(amortized_nanos) AS t, MAX(resource_tags) AS tags, MIN(usage_type) AS usage_type "
            f"FROM line_items WHERE {where} GROUP BY resource_id ORDER BY t DESC, resource_id LIMIT {MAX_RESOURCES}", params).fetchall()
        return {"rows": rows, "unsupported": engine._unsupported_amortized(self.conn, where, params),
                "total": engine._window_total(self.conn, q, q.start, q.end)}

    def _accounts(self, window: dict) -> list[str]:
        return [r["account_id"] for r in self.conn.execute(
            "SELECT DISTINCT account_id FROM line_items WHERE usage_date >= ? AND usage_date < ? ORDER BY account_id",
            (window["start"].isoformat(), window["end"].isoformat()))]

    def _raw(self, policy: dict, account: str, q: CostQuery, detail: str) -> dict | None:
        m = self._measure(q)
        if not m["rows"]:
            return None
        return {"policy": policy, "account_id": account, "query": q.to_dict(), "total": m["total"], "detail": detail,
                "unsupported": m["unsupported"], "rows": m["rows"]}

    def _evaluate(self, policy: dict, window: dict) -> list[dict]:
        kind, p = policy["kind"], policy["params"]
        found = []
        if kind == "required_tag":
            for a in self._accounts(window):
                q = self._query(window, {"account": [a], "line_item_type": USAGE_TYPES, f"tag:{p['key']}": [engine.UNTAGGED]})
                found.append(self._raw(policy, a, q, f"Resources without the tag {p['key']}."))
        elif kind == "allowed_regions":
            for a in self._accounts(window):
                regions = [r["region"] for r in self.conn.execute(
                    "SELECT DISTINCT region FROM line_items WHERE account_id=? AND usage_date >= ? AND usage_date < ? "
                    "ORDER BY region LIMIT 50", (a, window["start"].isoformat(), window["end"].isoformat()))]
                bad = [r for r in regions if r not in p["regions"] and r not in ("global", "")]
                if bad:
                    q = self._query(window, {"account": [a], "line_item_type": USAGE_TYPES, "region": bad})
                    found.append(self._raw(policy, a, q, f"Usage in regions outside the list: {', '.join(bad)}."))
        elif kind == "restricted_instance":
            families = re.compile(r"BoxUsage:(" + "|".join(re.escape(f) for f in p["families"]) + r")\.")
            for a in p["accounts"]:
                types = [r["usage_type"] for r in self.conn.execute(
                    "SELECT DISTINCT usage_type FROM line_items WHERE account_id=? AND usage_date >= ? AND usage_date < ? "
                    "AND usage_type LIKE '%BoxUsage%' ORDER BY usage_type LIMIT 200",
                    (a, window["start"].isoformat(), window["end"].isoformat()))]
                bad = [t for t in types if families.search(t)][:50]
                if bad:
                    q = self._query(window, {"account": [a], "usage_type": bad})
                    found.append(self._raw(policy, a, q, f"Restricted instance types: {', '.join(bad)}."))
        elif kind == "budget_coverage":
            for a in self._accounts(window):
                if not self._has_budget(a):
                    found.append(self._raw(policy, a, self._query(window, {"account": [a]}), "This account has no CostLens budget that covers all of its spend."))
        return [f for f in found if f]

    def _has_budget(self, account: str) -> bool:
        for r in self.conn.execute("SELECT spec_json FROM budgets"):
            filters = json.loads(r["spec_json"]).get("filters") or {}
            if not filters or (set(filters) == {"account"} and account in filters["account"]):
                return True
        return False


class ImpactWorker(_Worker):
    name = "impact"

    def _tools(self):
        return [Tool("first_seen", "First day a resource appears in any ingested CUR.", self._first_seen),
                Tool("optimize_links", "Optimize findings for the same resources.", self._links),
                Tool("simulate", "What a guardrail would have touched, from billing data.", self._simulate)]

    def _first_seen(self, account_id: str, resource_id: str) -> str | None:
        return self.conn.execute("SELECT MIN(usage_date) AS d FROM line_items WHERE account_id=? AND resource_id=?",
                                 (account_id, resource_id)).fetchone()["d"]

    def _links(self) -> dict:
        links: dict[tuple[str, str], list[dict]] = {}
        for f in optimize.summary(self.conn)["opportunities"]:
            links.setdefault((f["account_id"], f["resource_id"]), []).append(
                {"id": f["id"], "title": f["title"], "phase_label": f["phase_label"], "monthly_savings_display": f["monthly_savings_display"]})
        return links

    def _simulate(self, violations: list[dict], window: dict) -> dict:
        seen: dict[tuple[str, str], dict] = {}
        for v in violations:
            if v["spend_nanos"] is None:
                continue
            for r in v["resources"]:
                seen[(v["account_id"], r["resource_id"])] = r
        total = sum(r["spend_nanos"] for r in seen.values())
        new = sum(1 for r in seen.values() if r["first_seen"] and r["first_seen"] >= window["start"].isoformat())
        n = len(seen)
        if not n:
            summary = "No billed usage in the last 30 days would have been touched by this rule."
        else:
            summary = (f"In the last 30 days this rule touches {n} resource{'s' if n != 1 else ''}, {_fmt_usd(total)} of spend. "
                       f"{new} of them first appeared in that window. The rest were already running and would not be stopped.")
        return {"resources": n, "spend_nanos": total, "spend_display": _fmt_usd(total), "new_in_window": new, "summary": summary}


class GovernRiskWorker(_Worker):
    name = "risk"

    def _tools(self):
        return [Tool("assess_violation", "Limitations of one violation.", self._violation),
                Tool("assess_guardrail", "Risks and checks for one draft.", self._guardrail)]

    def _violation(self, v: dict, window: dict) -> dict:
        if v["spend_nanos"] is None:
            v["limitations"].insert(0, "Some charges cannot be amortized in this release, so no spend figure is given.")
        if not v["owner_known"]:
            v["limitations"].append("No team tag was found, so the account is shown as owner.")
        v["limitations"].append("This release reads CUR only. It cannot see resources that have no billed usage.")
        return v

    def _guardrail(self, g: dict, org: dict | None) -> dict:
        if g["type"] == "SCP":
            g["risks"] += ["A deny rule can break deployments that rely on the blocked action.",
                           "It blocks new actions only. Resources already running keep running."]
            g["checks"].append({"check": "Management account", "state": "warn",
                                "detail": "SCPs do not apply to the management account." if not g["hits_management"] else
                                "This draft targets the management account, where SCPs have no effect."})
            g["checks"].append({"check": "Service-linked roles", "state": "warn", "detail": "SCPs do not restrict service-linked roles."})
        else:
            g["risks"] += ["Enforced tag rules can make tagging calls fail for teams that are not ready."]
            g["checks"].append({"check": "Coverage", "state": "warn", "detail": "A tag policy controls allowed tag values. It does not force a tag to exist."})
        if g["impact"]["resources"] == 0:
            g["limitations"].append("Zero impact in billing does not prove zero impact: CUR does not show API calls.")
        g["limitations"].append("Format and size checks only. This draft has never been run against a real organization.")
        return g


class GuardrailWorker(_Worker):
    name = "guardrail"

    def _tools(self):
        return [Tool("draft", "Write one guardrail draft as text. It applies nothing.", self._draft)]

    def _document(self, policy: dict) -> tuple[str, dict] | None:
        sid = re.sub(r"[^A-Za-z0-9]", "", "CostLens" + policy["id"].title())
        if policy["kind"] == "restricted_instance":
            return "SCP", {"Version": "2012-10-17", "Statement": [{
                "Sid": sid, "Effect": "Deny", "Action": "ec2:RunInstances", "Resource": "arn:aws:ec2:*:*:instance/*",
                "Condition": {"StringLike": {"ec2:InstanceType": [f"{f}.*" for f in policy["params"]["families"]]}}}]}
        if policy["kind"] == "allowed_regions":
            return "SCP", {"Version": "2012-10-17", "Statement": [{
                "Sid": sid, "Effect": "Deny", "NotAction": GLOBAL_SERVICES, "Resource": "*",
                "Condition": {"StringNotEquals": {"aws:RequestedRegion": policy["params"]["regions"]}}}]}
        if policy["kind"] == "required_tag":
            key = policy["params"]["key"].removeprefix("user:")  # CUR adds the user: prefix; the AWS tag does not have it
            return "Tag policy", {"tags": {key: {"tag_key": {"@@assign": key}, "enforced_for": {"@@assign": ["ec2:instance"]}}}}
        return None

    def _targets(self, policy: dict, org: dict, names: dict) -> tuple[list[dict], list[str]]:
        scps = org.get("serviceControlPolicies", [])

        def attached(*ids: str) -> int:
            return sum(1 for s in scps if set(s["targets"]) & set(ids))

        ou_of = {a: ou for ou in org["organizationalUnits"] for a in ou["accounts"]}
        if policy["kind"] != "restricted_instance":
            return [{"id": "root", "name": "Organization root", "existing_scps": attached("root")}], []
        targets, blockers = [], []
        for a in policy["params"]["accounts"]:
            ou = ou_of.get(a)
            if not ou and a != org["managementAccount"]:
                blockers.append(f"Account {a} is not in the organization data.")
                continue
            targets.append({"id": a, "name": names.get(a, a), "ou": ou["name"] if ou else "Root",
                            "existing_scps": attached("root", a, *( [ou["id"]] if ou else []))})
        return targets, blockers

    def _draft(self, policy: dict, impact: dict, org: dict | None, names: dict, state: dict) -> dict | None:
        made = self._document(policy)
        if made is None:
            return None
        kind, document = made
        gid = _sha("guardrail", policy["id"])
        g = {"id": gid, "policy_id": policy["id"], "title": f"{kind}: {policy['name']}", "type": kind, "impact": impact,
             "document": None, "document_text": "", "targets": [], "checks": [], "blockers": [], "risks": [],
             "limitations": ["The impact is an estimate from billing data. CUR shows what was billed, not API calls."],
             "hits_management": False, "execution": NOT_APPLIED, "note": state.get("note", ""), "sha": ""}
        if org is None:
            g["blockers"].append("No organization data is loaded, so targets cannot be checked and no draft is written.")
        else:
            g["targets"], blockers = self._targets(policy, org, names)
            g["blockers"] += blockers
            g["hits_management"] = any(t["id"] == org["managementAccount"] for t in g["targets"])
        if not g["blockers"]:
            g["document"] = document
            g["document_text"] = json.dumps(document, indent=2, sort_keys=True)
            g["sha"] = hashlib.sha256((g["document_text"] + repr(g["targets"])).encode()).hexdigest()[:16]
            g["checks"] = check_document(document, kind)
            full = [t for t in g["targets"] if kind == "SCP" and t["existing_scps"] >= MAX_SCPS_PER_TARGET]
            if full:
                g["blockers"].append(f"{full[0]['name']} already has {MAX_SCPS_PER_TARGET} SCPs, the AWS limit.")
            g["blockers"] += [f"{c['check']} check failed: {c['detail']}" for c in g["checks"] if c["state"] == "fail"]
        where = ", ".join(t["name"] for t in g["targets"]) or "the target"
        g["rollout"] = ["Review with the owners of the target accounts.", "Attach to a test account or OU first, for at least 7 days.",
                        "Watch CloudTrail for denied calls.", f"Then attach to {where}."]
        g["rollback"] = [f"Detach it from {where}. The effect ends once the change has spread."]
        status = state.get("status", "draft")
        changed = status != "draft" and state.get("draft_sha") != g["sha"]
        g["status"] = "draft" if changed else status
        if changed:
            g["note"] = "The draft changed after review, so it went back to draft."
        g["status_label"] = STATUS_LABELS[g["status"]]
        g["next_statuses"] = [] if g["blockers"] else list(TRANSITIONS[g["status"]])
        return g


class GovernancePipeline:
    def __init__(self, conn):
        self.conn = conn
        self.trace: dict[str, int] = {}
        self.checks = PolicyCheckWorker(conn, self.trace)
        self.impact = ImpactWorker(conn, self.trace)
        self.risk = GovernRiskWorker(conn, self.trace)
        self.guardrails = GuardrailWorker(conn, self.trace)

    def _window(self) -> dict | None:
        last = self.conn.execute("SELECT MAX(usage_date) AS d FROM line_items").fetchone()["d"]
        if not last:
            return None
        through = date.fromisoformat(last)
        start, end = through - timedelta(days=WINDOW_DAYS - 1), through + timedelta(days=1)
        return {"start": start, "end": end, "notes": engine.coverage(self.conn, start, end)["notes"]}

    def _exceptions(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for r in self.conn.execute("SELECT * FROM gov_exceptions ORDER BY id"):
            latest[r["violation_id"]] = dict(r)
        return latest

    def _violation(self, raw: dict, names: dict, links: dict, exceptions: dict, ref: date, window: dict) -> dict:
        policy, account = raw["policy"], raw["account_id"]
        resources, team = [], None
        for r in raw["rows"]:
            rid = r["resource_id"] or "(no resource id)"
            tags = json.loads(r["tags"] or "{}")
            team = team or tags.get("user:team")
            resources.append({"resource_id": rid, "spend_nanos": r["t"] or 0, "spend_display": _fmt_usd(r["t"] or 0),
                              "usage_type": r["usage_type"], "optimize": links.get((account, r["resource_id"]), []),
                              "first_seen": self.impact.call("first_seen", account_id=account, resource_id=r["resource_id"])})
        spend = None if raw["unsupported"] else raw["total"]
        vid = _sha("violation", policy["id"], account)
        ex = exceptions.get(vid)
        active = bool(ex) and date.fromisoformat(ex["expires_on"]) >= ref
        name = names.get(account, account)
        v = {"id": vid, "title": f"{policy['name']}: {name}", "policy_id": policy["id"], "policy_name": policy["name"],
             "kind": policy["kind"], "account_id": account, "account": name, "detail": raw["detail"],
             "owner": team or name, "owner_source": "user:team tag in CUR" if team else "account (no team tag found)",
             "owner_known": bool(team), "spend_nanos": spend, "spend_display": _fmt_usd(spend) if spend is not None else "Not enough evidence",
             "resources": resources, "resource_count": len(resources), "evidence_query": raw["query"],
             "time_range": {"start": window["start"].isoformat(), "end": (window["end"] - timedelta(days=1)).isoformat()},
             "status": "excepted" if active else "open", "limitations": list(window["notes"]),
             "exception": ({"owner": ex["owner"], "reason": ex["reason"], "expires_on": ex["expires_on"], "active": active} if ex else None)}
        if ex and not active:
            v["limitations"].insert(0, f"An exception expired on {ex['expires_on']}, so this is open again.")
        if policy["kind"] == "budget_coverage":
            v["suggestion"] = f"Create a budget for {name} on the Budgets page. CostLens does not choose the amount."
        return v

    def run(self) -> dict:
        policies = self.checks.call("policies")
        window = self._window()
        raw_org = get_meta(self.conn, "org_structure")
        org = json.loads(raw_org) if raw_org else None
        if not policies or window is None:
            reason = "No governance policies are loaded." if not policies else "No CUR data has been ingested, so nothing can be checked."
            AuditLog(self.conn).record("govern.run", {"state": "no_data", "tools": self.trace})
            return {"state": "no_data", "reason": reason, "violations": [], "guardrails": [], "policies": policies,
                    "totals": _totals([], []), "workers": self.trace}
        names = account_names(self.conn)
        ref = engine.reference_date(self.conn)
        links = self.impact.call("optimize_links")
        exceptions = self._exceptions()
        states = {r["guardrail_id"]: dict(r) for r in self.conn.execute("SELECT * FROM gov_guardrail_states")}
        violations, guardrails = [], []
        for p in policies:
            if not p["enabled"]:
                p["result"] = "off"
                continue
            found = [self._violation(raw, names, links, exceptions, ref, window)
                     for raw in self.checks.call("evaluate", policy=p, window=window)]
            for v in found:
                self.risk.call("assess_violation", v=v, window=window)
            p["result"] = "violations" if found else "compliant"
            p["violation_count"] = len(found)
            violations += found
            impact = self.impact.call("simulate", violations=found, window=window)
            state = states.get(_sha("guardrail", p["id"]), {})
            g = self.guardrails.call("draft", policy=p, impact=impact, org=org, names=names, state=state)
            if g:
                guardrails.append(self.risk.call("assess_guardrail", g=g, org=org))
        violations.sort(key=lambda v: (v["status"] != "open", v["kind"] == "budget_coverage", -(v["spend_nanos"] or 0), v["id"]))
        AuditLog(self.conn).record("govern.run", {"state": "ok", "violations": len(violations), "tools": self.trace})
        return {"state": "ok", "violations": violations, "guardrails": guardrails, "policies": policies,
                "totals": _totals(violations, guardrails), "workers": self.trace, "organization_loaded": org is not None,
                "window": {"start": window["start"].isoformat(), "end": (window["end"] - timedelta(days=1)).isoformat(), "notes": window["notes"]},
                "reference_date": ref.isoformat(),
                "method": {
                    "Evidence": "Amortized cost over the last 30 days of CUR",
                    "Spend total": "Each resource counts once, even if it breaks several policies. Budget coverage is left out.",
                    "Impact": "An estimate from billing data. CUR does not show API calls, and guardrails do not stop running resources.",
                    "Approved": "A decision recorded in this app. There is no login yet, so it is not verified, and nothing changes in AWS.",
                }}


def _totals(violations: list[dict], guardrails: list[dict]) -> dict:
    open_items = [v for v in violations if v["status"] == "open"]
    unique: dict[tuple[str, str], int] = {}
    for v in open_items:
        if v["kind"] != "budget_coverage" and v["spend_nanos"] is not None:
            for r in v["resources"]:
                unique[(v["account_id"], r["resource_id"])] = r["spend_nanos"]
    spend = sum(unique.values())
    return {"open_count": len(open_items), "excepted_count": len(violations) - len(open_items),
            "spend_nanos": spend, "spend_display": _fmt_usd(spend), "resources": len(unique),
            "drafts": len(guardrails), "in_review": sum(1 for g in guardrails if g["status"] == "in_review"),
            "approved": sum(1 for g in guardrails if g["status"] == "approved")}


def summary(conn) -> dict:
    return GovernancePipeline(conn).run()


def set_guardrail_status(conn, guardrail_id: str, status: str, note: str = "") -> dict:
    g = next((x for x in summary(conn)["guardrails"] if x["id"] == guardrail_id), None)
    if g is None:
        raise ValueError("guardrail not found")
    if g["blockers"]:
        raise ValueError("this draft has blockers and cannot move forward")
    if status not in TRANSITIONS[g["status"]]:
        raise ValueError(f"cannot move from {g['status']} to {status}")
    conn.execute(
        "INSERT INTO gov_guardrail_states(guardrail_id, status, draft_sha, note, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(guardrail_id) DO UPDATE SET status=excluded.status, draft_sha=excluded.draft_sha, note=excluded.note, "
        "updated_at=excluded.updated_at", (guardrail_id, status, g["sha"], note[:500], utc_now()))
    return {"id": guardrail_id, "status": status, "label": STATUS_LABELS[status]}


def add_exception(conn, violation_id: str, owner: str, reason: str, expires_on: str) -> dict:
    owner, reason = owner.strip(), reason.strip()
    if not 0 < len(owner) <= 120:
        raise ValueError("owner must be 1-120 characters")
    if not 0 < len(reason) <= 500:
        raise ValueError("reason must be 1-500 characters")
    try:
        expiry = date.fromisoformat(expires_on)
    except ValueError as exc:
        raise ValueError("expiry must be YYYY-MM-DD") from exc
    ref = engine.reference_date(conn)
    if not ref < expiry <= ref + timedelta(days=MAX_EXCEPTION_DAYS):
        raise ValueError(f"expiry must be after {ref.isoformat()} and within {MAX_EXCEPTION_DAYS} days")
    if not any(v["id"] == violation_id for v in summary(conn)["violations"]):
        raise ValueError("violation not found")
    conn.execute("INSERT INTO gov_exceptions(violation_id, owner, reason, expires_on, created_at) VALUES (?,?,?,?,?)",
                 (violation_id, owner, reason, expiry.isoformat(), utc_now()))
    return {"id": violation_id, "expires_on": expiry.isoformat()}


def set_policy_enabled(conn, policy_id: str, enabled: bool) -> None:
    if conn.execute("UPDATE gov_policies SET enabled=?, updated_at=? WHERE id=?",
                    (1 if enabled else 0, utc_now(), policy_id)).rowcount == 0:
        raise ValueError("policy not found")


def answer(conn, understood: list[str], filters: dict, text: str) -> dict:
    """The chat reply. Every number comes from the same pipeline as the Govern page."""
    s = summary(conn)
    reply = {"kind": "govern", "worker": "govern", "state": s["state"], "understood": understood, "totals": s["totals"],
             "violations": [], "guardrails": [], "suggestions": []}
    if s["state"] != "ok":
        reply["message"] = f"{s['reason']} I will not guess."
        return reply
    accounts = filters.get("account")
    violations = [v for v in s["violations"] if not accounts or v["account_id"] in accounts]
    guardrails = s["guardrails"]
    lower = text.lower()
    if re.search(r"\bgpus?\b", lower):
        guardrails = [g for g in guardrails if g["policy_id"] in {p["id"] for p in s["policies"] if p["kind"] == "restricted_instance"}]
    if accounts:
        guardrails = [g for g in guardrails if g["targets"] and any(t["id"] in accounts or t["id"] == "root" for t in g["targets"])]
    wants_draft = bool(re.search(r"\b(draft|guardrails?|scps?|block|enforce\w*|tag polic\w*)\b", lower))
    no_owner = bool(re.search(r"\b(no|without( an?)?) owner|who owns|untagged\b", lower))
    if no_owner:
        violations = [v for v in violations if v["kind"] == "required_tag"]
    # Only accounts narrow the results, so only accounts may be named as the scope.
    names = [u for u in understood if u.startswith("account ")]
    scope = account_scope = f" for {', '.join(names)}" if names else ""
    if wants_draft:
        reply["guardrails"] = guardrails[:3]
        if not guardrails:
            reply["message"] = f"I have no guardrail draft{scope}. A policy must exist first, so I will not write one from nothing."
        else:
            detail = guardrails[0]["impact"]["summary"] if len(guardrails) == 1 else "Each draft shows its own impact."
            reply["message"] = (f"Here {'is a draft' if len(guardrails) == 1 else f'are {len(guardrails)} drafts'}{account_scope}. "
                                f"{detail} {NOT_APPLIED}")
    else:
        open_items = [v for v in violations if v["status"] == "open"]
        reply["violations"] = violations[:5]
        if not violations:
            reply["message"] = f"No policy violations{scope} in the last 30 days of billing data. CUR cannot see resources with no billed usage."
        elif no_owner:
            spend = _totals(open_items, [])
            reply["message"] = (f"{spend['spend_display']} of spend across {spend['resources']} resources has no team tag{scope} "
                                "in the last 30 days, so its owner is not known.")
        else:
            spend = _totals(open_items, [])
            reply["message"] = (f"{len(open_items)} open violation{'s' if len(open_items) != 1 else ''}{scope}. "
                                f"They touch {spend['spend_display']} of spend in the last 30 days, each resource counted once. "
                                "Budget gaps are listed but not added to that figure.")
    reply["suggestions"] = ["Which spend has no owner?", "Draft a guardrail for GPUs in sandbox", "Show policy violations"]
    return reply
