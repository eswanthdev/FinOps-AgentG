"""AgentCore HTTP entrypoint: IAM-protected runtime -> signed MCP -> Athena.

No durable state is kept here. Caller application owns conversations/reports.
"""
from __future__ import annotations

import json
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..store import connect
from .cloud_tools import MODEL_SPECS
from .investigator import SYSTEM_PROMPT, investigate, make_client, reply
from .mcp_client import GatewayTools


def invoke(payload: dict, *, tools=None, client=None):
    if not isinstance(payload, dict):
        raise ValueError("invocation must be an object")
    operation = payload.get("operation","ask")
    allowed = {"ask":{"question","previous","operation","history","notes"},"health":{"operation"},
               "query":{"operation","query"},"dimensions":{"operation"},"status":{"operation"},"optimization":{"operation"},"opportunities":{"operation","account"}}
    if operation not in allowed or set(payload)-allowed[operation]:
        raise ValueError("unsupported invocation fields")
    tools = tools or GatewayTools(os.environ["COSTLENS_GATEWAY_URL"], os.environ["AWS_REGION"])
    tools.initialize()
    if payload.get("operation") == "health":
        return {"status": "ok", "tools": tools.discover(), "synthetic_data": os.environ.get("COSTLENS_SYNTHETIC", "true") == "true"}
    if operation in ("query","dimensions","status","opportunities","optimization"):
        name = {"query":"dashboard_query","dimensions":"dashboard_dimensions","status":"dashboard_status","optimization":"dashboard_optimization","opportunities":"list_opportunities"}[operation]
        arguments = {"query":payload.get("query")} if operation=="query" else {"account":payload.get("account","")} if operation=="opportunities" else {}
        return {"status":"ok","result":tools.call(name,**arguments)}
    question = payload.get("question")
    if not isinstance(question, str) or not 0 < len(question.strip()) <= 2000:
        raise ValueError("question must be 1-2000 characters")
    history = payload.get("history",[])
    notes = payload.get("notes",[])
    if (not isinstance(history,list) or len(history)>16 or any(not isinstance(m,dict) or set(m)!={"role","content"}
        or m["role"] not in ("user","assistant") or not isinstance(m["content"],str) for m in history)
        or sum(len(m["content"]) for m in history)>12_000 or not isinstance(notes,list) or len(notes)>50
        or any(not isinstance(note,str) or len(note)>300 for note in notes)):
        raise ValueError("invalid bounded conversation context")
    client = client or make_client()
    if client is None:
        return {"status": "unavailable", "message": "No supported model is configured"}
    conn = connect(":memory:")
    try:
        result = investigate(conn, client, question, payload.get("previous"), tool_backend=tools,history=history,notes=notes,
                             tool_specs=[{**spec, "strict": True} for spec in MODEL_SPECS],
                             reference_date=date.fromisoformat(os.environ["COSTLENS_REFERENCE_DATE"]),
                             system_prompt=SYSTEM_PROMPT + "\nUse list_opportunities first for savings questions. It is the common deduplicated view. "
                             "list_recommendations is raw supporting evidence, not extra savings. Canonical claims are still unvalidated. "
                             "Never sum alternatives or source claims. No realised-savings, governance or budget tool is connected in this cloud pilot. "
                             "Dates use an inclusive start and EXCLUSIVE end. A full month ends on the first day of the next month. "
                             "For total bills call query_costs with granularity total and group_by service, then copy total_display. "
                             "If a tool rejects your input, correct it and retry. Unknown billing finality does not mean not-final.")
        if result["status"] != "ok":
            return {"status": result["status"], "message": result["note"], "trace": result["trace"]}
        return {"status": "ok", **reply(result), "synthetic_data": os.environ.get("COSTLENS_SYNTHETIC", "true") == "true",
                "mode": {"mode": "model", "label": "AI-assisted mode", "detail": "AgentCore with scoped Athena tools through IAM-authenticated MCP"},
                "tokens": result["tokens"], "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"]}
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_json(200 if self.path == "/ping" else 404, {"status": "Healthy" if self.path == "/ping" else "not_found"})

    def do_POST(self):
        if self.path != "/invocations":
            return self.send_json(404, {"status": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65_536:
                raise ValueError("invalid request size")
            self.send_json(200, invoke(json.loads(self.rfile.read(length))))
        except (ValueError, TypeError):
            self.send_json(400, {"status": "rejected", "message": "Invalid invocation"})
        except Exception as exc:
            print(json.dumps({"event": "cloud_runtime_error", "type": type(exc).__name__}), flush=True)
            self.send_json(500, {"status": "unavailable", "message": "Cloud investigation failed; no unverified answer was returned"})


def main():
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
