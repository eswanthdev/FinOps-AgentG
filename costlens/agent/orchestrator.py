"""Orchestrator and specialist workers.

Implemented now: the Visualization worker (cost exploration and variance) here,
the Cost Optimization Engineer workers in agent/optimize.py and the Governance
workers in agent/govern.py. PLANNED_WORKERS lists anything declared but not built.

Workers only reach data through bounded, typed tools. In this release no model
is called: the orchestrator is deterministic code and says so in every reply.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from ..query import engine
from ..query.model import CostQuery, QueryError
from ..store import utc_now
from .interpreter import DeterministicInterpreter, Interpretation, Vocabulary

PLANNED_WORKERS: dict[str, str] = {}
ACTIVE_WORKERS = {
    "visualization": "Cost exploration and change analysis",
    "optimize": "Cost Optimization Engineer: validated savings estimates and reviewable plans (read-only, makes no changes)",
    "govern": "Governance: policy checks and draft guardrails (drafts only, applies nothing)",
    "anomalies": "Deterministic daily anomaly detection",
    "budgets": "Budgets, run-rate forecasts and budget alerts (CostLens budgets, not AWS Budgets)",
}
_GOVERN_RE = re.compile(r"\b(scps?|guardrails?|governance|polic(y|ies)|violations?|complian\w*|enforce\w*|limit spend|"
                        r"(no|without( an?)?) owner|who owns)\b", re.I)
_REALISED_RE = re.compile(r"\b(reali[sz]ed sav\w*|actual sav\w*|how much (have|did) (we|i) sav\w*)\b", re.I)
_OPTIMIZE_RE = re.compile(r"\b(optimi[sz]\w*|rightsiz\w*|right-siz\w*|waste\w*|idle|save money|sav\w+ opportunit\w*|"
                          r"how (much )?(can|could|do|should) (we|i|\S+) save|"
                          r"(reduce|reducing|lower|cut|trim)\b[^.?!]*\b(costs?|spend\w*|bills?))\b", re.I)
_ANOMALY_RE = re.compile(r"\b(anomal\w*|unusual|unexpected|abnormal|outliers?)\b", re.I)
_FORECAST_RE = re.compile(r"\b(forecast\w*|predict\w*|projection|projected|end of (the )?(month|quarter|year)|"
                          r"run[- ]?rate|where will .* land)\b", re.I)
_LIST_BUDGETS_RE = re.compile(r"\b(list|show|what are|which|my|all)\b.*\bbudgets\b|^budgets\??$", re.I)

_SECRET_PATTERNS = [
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(aws_secret_access_key|secret|token|password)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9/+=]{40}\b"),
]


def redact(text: str) -> str:
    for p in _SECRET_PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


def model_mode() -> dict:
    provider = os.environ.get("COSTLENS_MODEL_PROVIDER", "").strip().lower()
    if provider in ("bedrock", "bedrock-nova"):
        if importlib.util.find_spec("boto3" if provider == "bedrock-nova" else "anthropic"):
            return {"mode": "model", "label": "AI-assisted mode",
                    "detail": "A model chooses read-only tools and writes the answer. Every number comes from code. "
                              "If the model fails, answers fall back to fixed rules."}
        return {"mode": "deterministic", "label": "Deterministic mode",
                "detail": "Bedrock is selected but the optional package is not installed. Running fixed rules only."}
    if provider:
        return {"mode": "deterministic", "label": "Deterministic mode",
                "detail": f"Model provider '{provider}' is not supported. Running deterministic rules only."}
    return {"mode": "deterministic", "label": "Deterministic mode",
            "detail": "No AI model is configured. Questions are interpreted by fixed rules and every number is "
                      "computed by code."}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]


class AuditLog:
    def __init__(self, conn):
        self.conn = conn

    def record(self, event_type: str, detail: dict) -> None:
        self.conn.execute("INSERT INTO audit_events(ts, event_type, detail) VALUES (?,?,?)",
                          (utc_now(), event_type, json.dumps(detail, sort_keys=True, default=str)))


class VisualizationWorker:
    name = "visualization"

    def __init__(self, conn, audit: AuditLog):
        self.conn = conn
        self.audit = audit
        self.tools = {
            "query_costs": Tool("query_costs", "Run a validated CostQuery against ingested CUR data.", self._query_costs),
            "data_coverage": Tool("data_coverage", "Coverage and freshness for a date range.", self._coverage),
        }

    def _query_costs(self, q: CostQuery) -> dict:
        if not isinstance(q, CostQuery):
            raise TypeError("query_costs takes a CostQuery")
        q.validate()
        return engine.run(self.conn, q)

    def _coverage(self, start: date, end: date) -> dict:
        return engine.coverage(self.conn, start, end)

    def call(self, tool: str, **kwargs) -> Any:
        if tool not in self.tools:
            raise PermissionError(f"tool {tool!r} is not available to the {self.name} worker")
        result = self.tools[tool].handler(**kwargs)
        self.audit.record("tool.call", {"worker": self.name, "tool": tool,
                                         "status": result.get("status") if isinstance(result, dict) else None})
        return result

    def follow_ups(self, result: dict) -> list[str]:
        if result.get("status") != "ok":
            return []
        q = result["query"]
        out = []
        if result["kind"] == "variance":
            groups = result["drivers"]["groups"]
            if groups:
                g = groups[0]
                out.append(f"Daily cost for usage type {g['usage_type']} in the last 30 days")
            out.append("Show the same change by account")
            out.append("Show amortized cost by service last month")
        else:
            series = result.get("series") or []
            if q["group_by"] == "service" and series and series[0]["key"] not in ("Other",):
                out.append(f"Break down {series[0]['key']} by usage type")
            if q["group_by"] != "account":
                out.append("Now by account")
            out.append("Why did it change vs the previous period?")
            if q["metric"] == "unblended":
                out.append("Show it as amortized cost")
        return out[:4]


class Orchestrator:
    def __init__(self, conn):
        self.conn = conn
        self.audit = AuditLog(conn)
        self.interpreter = DeterministicInterpreter()
        self.visualization = VisualizationWorker(conn, self.audit)

    def vocabulary(self) -> Vocabulary:
        dv = engine.dimension_values(self.conn, limit=200)
        return Vocabulary(
            services=[x["value"] for x in dv["service"]],
            accounts=engine.account_names(self.conn),
            regions=[x["value"] for x in dv["region"]],
            usage_types=[x["value"] for x in dv["usage_type"]],
            tags=dv["tags"],
        )

    def _special(self, question: str) -> dict | None:
        """Route budget, anomaly and forecast requests to their workers."""
        from .. import anomalies, budgets
        text = question.strip()
        ref = engine.reference_date(self.conn)
        if budgets.is_budget_request(text):
            parsed = budgets.parse_budget(text, self.vocabulary(), ref)
            self.audit.record("tool.call", {"worker": "budgets", "tool": "parse_budget", "status": parsed["status"]})
            if parsed["status"] != "ok":
                return {"kind": "budget_clarify", "message": parsed["message"], "worker": "budgets",
                        "suggestions": ["Set up a monthly budget of $5,000 for prod-data and alert ops@example.com at 80% and 100%"]}
            return {"kind": "budget_preview", "worker": "budgets", "preview": parsed,
                    "message": "Here is the budget I understood. Check it, then press Create budget. Nothing is saved until you do."}
        if _LIST_BUDGETS_RE.search(text):
            items = budgets.list_status(self.conn)
            return {"kind": "budget_list", "worker": "budgets", "budgets": items,
                    "message": f"You have {len(items)} budget{'s' if len(items) != 1 else ''}." if items
                    else "No budgets yet. Describe one in plain English, for example: set up a monthly budget of $5,000 for prod-data."}
        if _GOVERN_RE.search(text):
            from . import govern
            understood: list[str] = []
            filters = self.interpreter._filters(text.lower(), self.vocabulary(), understood)
            return govern.answer(self.conn, understood, filters, text)
        realised = bool(_REALISED_RE.search(text))
        if realised or _OPTIMIZE_RE.search(text):
            from . import optimize
            understood: list[str] = []
            filters = self.interpreter._filters(text.lower(), self.vocabulary(), understood)
            return optimize.answer(self.conn, understood, filters, realised)
        if _ANOMALY_RE.search(text):
            summ = anomalies.summary(self.conn)
            self.audit.record("tool.call", {"worker": "anomalies", "tool": "detect", "status": "ok"})
            items = summ["anomalies"]
            open_items = [a for a in items if a["status"] == "open"]
            if not items:
                msg = "No cost anomalies found in the ingested data with the current detection rules."
            else:
                top = items[0]
                msg = (f"I found {len(items)} cost anomal{'ies' if len(items) != 1 else 'y'} ({len(open_items)} open). "
                       f"The largest: {top['service']} in {top['account']} from {top['start']}, "
                       f"{top['impact_display']} above expected. {top['explanation']}")
            return {"kind": "anomalies", "worker": "anomalies", "anomalies": items[:5], "total": len(items),
                    "method": summ["method"], "message": msg}
        if _FORECAST_RE.search(text):
            if re.search(r"\bnext (month|quarter|year)\b", text, re.I):
                return {"kind": "forecast_clarify", "worker": "budgets",
                        "message": "This release forecasts only to the end of the current month, quarter or year.",
                        "suggestions": ["Forecast this month's cost", "Forecast prod-data cost for this quarter"]}
            understood: list[str] = []
            filters = self.interpreter._filters(text.lower(), self.vocabulary(), understood)
            period = "quarterly" if re.search(r"quarter", text, re.I) else "yearly" if re.search(r"\byear", text, re.I) else "monthly"
            metric = "amortized" if re.search(r"amorti[sz]ed", text, re.I) else "unblended"
            start, end = budgets.period_window(period, ref)
            fc = budgets.forecast(self.conn, filters, metric, start, end)
            self.audit.record("tool.call", {"worker": "budgets", "tool": "forecast", "status": fc["status"]})
            fc.update({"filters": filters, "metric": metric, "period": period,
                       "period_label": budgets.period_label(period, start), "understood": understood})
            if fc["status"] != "ok":
                return {"kind": "forecast", "worker": "budgets", "forecast": fc, "message": fc["reason"]}
            return {"kind": "forecast", "worker": "budgets", "forecast": fc,
                    "message": (f"Estimated {metric} cost for {fc['period_label']}: {fc['forecast_display']} "
                                f"(range {fc['low_display']} – {fc['high_display']}, {fc['confidence']} confidence). "
                                f"Spent so far: {fc['actual_display']}."),
                    "suggestions": [self._budget_suggestion(fc, filters, period), "Show cost by service this month"]}
        return None

    def _budget_suggestion(self, fc: dict, filters: dict, period: str) -> str:
        """A ready-to-use budget sentence: forecast rounded up to the next $1,000."""
        dollars = -(-fc["forecast_nanos"] // (1000 * 1_000_000_000)) * 1000
        names = engine.account_names(self.conn)
        parts = []
        for dim, vals in filters.items():
            for v in vals:
                parts.append(names.get(v, v) if dim == "account" else v)
        scope = f" for {' and '.join(parts)}" if parts else ""
        return f"Set up a {period} budget of ${dollars:,}{scope} and alert me at 80% and 100%"

    def ask(self, question: str, previous: dict | None = None, *, user_id: str = "local", conversation_id: str | None = None,
            on_event=None) -> dict:
        """Answer inside a saved conversation. An empty question is answered but not saved."""
        from . import session
        question = (question or "")[:2000]
        if not question.strip():
            return self._ask_deterministic(question, previous)
        conversation = session.ensure_conversation(self.conn, user_id, conversation_id, question)
        past = session.history(self.conn, user_id, conversation)
        session.add_message(self.conn, conversation, "user", question)
        response = self._answer(question, previous, user_id, conversation, past, on_event)
        session.add_message(self.conn, conversation, "assistant", str(response.get("message") or ""), response.get("run_id"))
        response["conversation_id"] = conversation
        return response

    def _answer(self, question: str, previous: dict | None, user_id: str, conversation: str, past: list[dict], on_event) -> dict:
        """Use the investigator when a model is configured. Budget creation stays on its reviewed preview flow."""
        from .. import budgets
        from . import investigator, session
        wanted = session.remember_request(question)
        if wanted:
            try:
                session.add_memory(self.conn, user_id, wanted)
                message = "Saved to your memory. You can see or delete it on the Memory page."
            except session.SessionError as exc:
                message = f"I did not save that: {exc}"
            return {"kind": "memory", "worker": "memory", "message": message, "suggestions": [], "mode": model_mode(), "result": None,
                    "interpretation": {"status": "ok", "module": "memory", "understood": [], "assumptions": [], "query": None}}
        if budgets.is_budget_request(question):
            return self._ask_deterministic(question, previous)
        client = investigator.make_client()
        if client is None:
            return self._ask_deterministic(question, previous)
        if session.tokens_this_month(self.conn) >= session.monthly_token_cap():
            response = self._ask_deterministic(question, previous)
            response["model_note"] = "The monthly model limit has been reached. This answer comes from fixed rules."
            return response
        notes = [m["text"] for m in session.list_memories(self.conn, user_id)]
        started = time.monotonic()
        result = investigator.investigate(self.conn, client, question, previous, history=past, notes=notes, on_event=on_event)
        run_id = session.record_run(self.conn, user_id, conversation, investigator.model_name(client), question, result,
                                    int((time.monotonic() - started) * 1000))
        if result["status"] == "ok":
            self.audit.record("question.received", {"question": redact(question[:500]), "interpretation": "investigation",
                                                    "module": "investigator"})
            response = investigator.reply(result)
            response["mode"] = model_mode()
            response["interpretation"] = {"status": "ok", "module": "investigator", "understood": [], "assumptions": [], "query": None}
            response["result"] = None
            response["run_id"] = run_id
            response["memories_used"] = len(notes)
            return response
        response = self._ask_deterministic(question, previous)
        response["model_note"] = f"{result['note']} This answer comes from fixed rules."
        response["mode"] = {"mode": "deterministic", "label": "Deterministic mode", "detail": result["note"]}
        return response

    def _ask_deterministic(self, question: str, previous: dict | None = None) -> dict:
        qhash = hashlib.sha256(question.encode()).hexdigest()[:16]
        special = self._special(question) if question.strip() else None
        if special:
            self.audit.record("question.received", {"question": redact(question[:500]), "sha256_16": qhash,
                                                    "interpretation": special["kind"], "module": special["worker"]})
            special.setdefault("suggestions", [])
            special["mode"] = model_mode()
            special["interpretation"] = {"status": "ok", "module": special["worker"], "understood": [],
                                         "assumptions": [], "query": None}
            special["result"] = None
            return special
        prev_q = None
        if previous:
            try:
                prev_q = CostQuery.from_dict(previous)
            except QueryError:
                prev_q = None
        interp: Interpretation = self.interpreter.interpret(question, self.vocabulary(),
                                                            engine.reference_date(self.conn), prev_q)
        self.audit.record("question.received", {"question": redact(question[:500]), "sha256_16": qhash,
                                                "interpretation": interp.status, "module": interp.module})
        response = {"mode": model_mode(), "interpretation": interp.to_dict(), "worker": None, "result": None}
        if interp.status != "ok":
            response["message"] = interp.message
            response["suggestions"] = interp.suggestions
            return response
        result = self.visualization.call("query_costs", q=interp.query)
        response["worker"] = self.visualization.name
        response["result"] = result
        response["message"] = result.get("summary") or result.get("reason", "")
        response["suggestions"] = self.visualization.follow_ups(result)
        return response

    def run_query(self, query: dict) -> dict:
        q = CostQuery.from_dict(query)
        self.audit.record("query.direct", {"query": q.to_dict()})
        result = self.visualization.call("query_costs", q=q)
        return {"mode": model_mode(), "result": result, "worker": self.visualization.name,
                "suggestions": self.visualization.follow_ups(result)}
