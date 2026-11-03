"""Orchestrator and specialist workers.

Implemented now: the Visualization worker (cost exploration and variance).
Planned workers (Optimization, Risk, Action planning, Savings verification,
Governance) are declared in PLANNED_WORKERS so the UI can show them honestly;
they have no implementation and are never invoked.

Workers only reach data through bounded, typed tools. In this release no model
is called: the orchestrator is deterministic code and says so in every reply.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from ..query import engine
from ..query.model import CostQuery, QueryError
from ..store import utc_now
from .interpreter import DeterministicInterpreter, Interpretation, Vocabulary

PLANNED_WORKERS = {
    "optimize": "Cost Optimization Engineer: finds, validates and plans savings (next phase).",
    "govern": "Governance: budgets, alerts and approved guardrails such as SCP proposals (later phase).",
}

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
    provider = os.environ.get("COSTLENS_MODEL_PROVIDER", "").strip()
    if provider:
        return {"mode": "deterministic", "label": "Deterministic mode",
                "detail": f"Model provider '{provider}' is configured but model-backed interpretation is not "
                          "implemented in this release. Running deterministic rules only."}
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

    def ask(self, question: str, previous: dict | None = None) -> dict:
        question = (question or "")[:2000]
        qhash = hashlib.sha256(question.encode()).hexdigest()[:16]
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
