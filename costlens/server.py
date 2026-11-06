"""Local HTTP server (Python standard library only).

Binds to 127.0.0.1 by default. Defences for a local tool:
  * Host header must be localhost/127.0.0.1/[::1], or a name in
    COSTLENS_ALLOWED_HOSTS (blocks DNS-rebinding). See hosting.py.
  * POST/DELETE require Content-Type: application/json and a same-origin Origin
    when one is sent (blocks cross-site form posts).
  * Request bodies are capped; errors never include stack traces.
  * Strict Content-Security-Policy; the UI loads nothing from the internet.
"""
from __future__ import annotations

import json
import logging
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import anomalies, budgets, hosting, notify, cloud_app
from .agent import govern, optimize, session, verify
from .agent.orchestrator import ACTIVE_WORKERS, Orchestrator, PLANNED_WORKERS, model_mode
from .query import engine
from .query.model import CostQuery, QueryError
from .status import data_status
from .store import connect, utc_now

WEB_DIR = Path(__file__).parent / "web"
STATIC = {"app.js": "text/javascript", "styles.css": "text/css", "charts.js": "text/javascript", "features.js": "text/javascript",
          "optimize.js": "text/javascript", "govern.js": "text/javascript",
          "investigate.js": "text/javascript", "agent.js": "text/javascript"}
MAX_BODY = 64 * 1024
_HOST_RE = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
log = logging.getLogger("costlens.server")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def make_handler(db_path: str, source_label: str):
    extra_hosts = hosting.allowed_hosts()

    class Handler(BaseHTTPRequestHandler):
        server_version = "CostLens"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep request logs free of bodies
            log.info("%s %s", self.command, self.path.split("?")[0])

        # ---------------------------------------------------------- plumbing
        def _headers(self, status: int, ctype: str, length: int | None, cache: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:  # None streams until the connection closes
                self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-cache" if cache else "no-store")
            if extra_hosts:  # only ever reached over HTTPS through the login proxy
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.end_headers()

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload, default=str).encode()
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _check_host(self):
            host = self.headers.get("Host", "")
            if not _HOST_RE.match(host) and host.rsplit(":", 1)[0].lower() not in extra_hosts:
                raise ApiError(421, "Requests must use an allowed host.")

        def _body(self) -> dict:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ApiError(415, "Content-Type must be application/json")
            origin = self.headers.get("Origin")
            if origin:
                host = urlparse(origin).netloc
                if host != self.headers.get("Host"):
                    raise ApiError(403, "Cross-origin requests are not allowed.")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(413, "Request body too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ApiError(400, "Body is not valid JSON.") from exc
            if not isinstance(data, dict):
                raise ApiError(400, "Body must be a JSON object.")
            return data

        def _user(self) -> str:
            user = hosting.current_user(self.headers)
            if user is None:
                raise ApiError(401, "Sign in first.")
            return user

        def _stream_ask(self, orch, user: str, data: dict) -> None:
            """Send progress as one JSON object per line, then the full response."""
            self._headers(200, "application/x-ndjson; charset=utf-8", None)

            def send(event: dict) -> None:
                self.wfile.write(json.dumps(event, default=str).encode() + b"\n")
                self.wfile.flush()
            try:
                response = orch.ask(data["question"], data.get("previous") if isinstance(data.get("previous"), dict) else None,
                                    user_id=user, conversation_id=str(data.get("conversation_id") or "") or None, on_event=send)
                send({"type": "done", "response": response})
            except (BrokenPipeError, ConnectionResetError):
                log.info("client left during a stream")
            except cloud_app.CloudUnavailable as exc:
                send({"type":"error","error":str(exc)})
            except Exception:  # noqa: BLE001 - headers are sent, so report inside the stream
                log.error("unhandled stream error: %s", traceback.format_exc(limit=3))
                send({"type": "error", "error": "Internal error. Check the server log."})

        def _dispatch(self, fn):
            conn = None
            try:
                self._check_host()
                if cloud_app.enabled() and urlparse(self.path).path.startswith("/api/"):
                    self._user()
                conn = connect(db_path)
                fn(conn)
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except QueryError as exc:
                self._json(400, {"error": str(exc)})
            except cloud_app.CloudUnavailable as exc:
                self._json(503,{"error":str(exc)})
            except Exception:  # noqa: BLE001
                log.error("unhandled error: %s", traceback.format_exc(limit=3))
                self._json(500, {"error": "Internal error. Check the server log."})
            finally:
                if conn is not None:
                    conn.close()

        # ---------------------------------------------------------- routes
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz":
                # Load balancers call this by IP, so it skips the host check. It reveals no data.
                try:
                    conn = connect(db_path)
                    conn.execute("SELECT 1").fetchone()
                    conn.close()
                except Exception:  # noqa: BLE001
                    return self._json(503, {"status": "unavailable"})
                return self._json(200, {"status": "ok"})
            if path == "/" or path == "/index.html":
                try:
                    self._check_host()
                except ApiError as exc:
                    return self._json(exc.status, {"error": exc.message})
                body = (WEB_DIR / "index.html").read_bytes()
                self._headers(200, "text/html; charset=utf-8", len(body), cache=True)
                return self.wfile.write(body)
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name not in STATIC:
                    return self._json(404, {"error": "Not found"})
                body = (WEB_DIR / name).read_bytes()
                self._headers(200, STATIC[name] + "; charset=utf-8", len(body), cache=True)
                return self.wfile.write(body)

            def route(conn):
                def optimization_service():
                    values=parse_qs(urlparse(self.path).query).get("service",["all"])
                    if len(values)!=1:
                        raise QueryError("select one optimization service")
                    return values[0]
                if path == "/api/status":
                    s = cloud_app.status() if cloud_app.enabled() else data_status(conn, source_label)
                    s["model"] = model_mode()
                    s["planned_modules"] = PLANNED_WORKERS
                    s["active_modules"] = ACTIVE_WORKERS
                    s["email"] = notify.describe()
                    return self._json(200, s)
                if cloud_app.enabled() and path == "/api/opportunities":
                    self._user()
                    return self._json(200,cloud_app.opportunities())
                if cloud_app.enabled() and path=="/api/optimize":
                    self._user()
                    from .optimization_view import for_service
                    return self._json(200,for_service(cloud_app.optimization(),optimization_service()))
                if cloud_app.enabled() and path in ("/api/anomalies","/api/optimize","/api/govern","/api/budgets"):
                    raise ApiError(501,"This module is not connected to the cloud backend yet.")
                if path == "/api/anomalies":
                    return self._json(200, anomalies.summary(conn))
                if path == "/api/optimize":
                    from .optimization_view import for_service
                    return self._json(200,for_service(optimize.summary(conn),optimization_service()))
                if path == "/api/govern":
                    return self._json(200, govern.summary(conn))
                user = self._user()
                if path == "/api/conversations":
                    return self._json(200, {"conversations": session.list_conversations(conn, user)})
                m = re.match(r"^/api/conversations/([0-9a-f]{16})$", path)
                if m:
                    found = session.get_conversation(conn, user, m.group(1))
                    if not found:
                        raise ApiError(404, "Conversation not found")
                    return self._json(200, found)
                if path == "/api/memories":
                    return self._json(200, {"memories": session.list_memories(conn, user)})
                if path == "/api/reports":
                    return self._json(200, {"reports": session.list_reports(conn, user)})
                m = re.match(r"^/api/reports/(\d+)$", path)
                if m:
                    found = session.get_report(conn, user, int(m.group(1)))
                    if not found:
                        raise ApiError(404, "Report not found")
                    return self._json(200, found)
                if path == "/api/agent/runs":
                    return self._json(200, session.runs_summary(conn))
                if path == "/api/budgets":
                    return self._json(200, {"budgets": budgets.list_status(conn), "outbox": budgets.outbox(conn),
                                            "email": notify.describe(), "forecast": budgets.org_forecast(conn)})
                if path == "/api/dimensions":
                    return self._json(200, cloud_app.dimensions() if cloud_app.enabled() else engine.dimension_values(conn))
                if path == "/api/pins":
                    pins = [{"id": r["id"], "title": r["title"], "query": json.loads(r["query_json"]),
                             "created_at": r["created_at"]}
                            for r in conn.execute("SELECT * FROM pins ORDER BY id DESC")]
                    return self._json(200, {"pins": pins})
                if path == "/api/audit":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT ts, event_type, detail FROM audit_events ORDER BY id DESC LIMIT 100")]
                    return self._json(200, {"events": rows})
                raise ApiError(404, "Not found")
            self._dispatch(route)

        def do_POST(self):
            path = urlparse(self.path).path

            def route(conn):
                data = self._body()
                orch = Orchestrator(conn)
                user = self._user()
                if cloud_app.enabled() and any(path.startswith(prefix) for prefix in ("/api/optimize/","/api/govern/","/api/anomalies/","/api/budgets")):
                    raise ApiError(501,"This workflow is not connected to the cloud backend yet.")
                if path in ("/api/ask", "/api/ask/stream"):
                    q = data.get("question")
                    if not isinstance(q, str):
                        raise ApiError(400, "question must be a string")
                    if path == "/api/ask/stream":
                        return self._stream_ask(orch, user, data)
                    prev = data.get("previous")
                    return self._json(200, orch.ask(q, prev if isinstance(prev, dict) else None, user_id=user,
                                                    conversation_id=str(data.get("conversation_id") or "") or None))
                if path == "/api/memories":
                    try:
                        return self._json(201, session.add_memory(conn, user, str(data.get("text", ""))))
                    except session.SessionError as exc:
                        raise ApiError(400, str(exc)) from exc
                if path == "/api/reports":
                    try:
                        report = session.save_report(conn, user, int(data.get("run_id") or 0), str(data.get("title") or ""))
                    except (session.SessionError, ValueError) as exc:
                        raise ApiError(400, str(exc)) from exc
                    return self._json(201, {"id": report})
                m = re.match(r"^/api/agent/runs/(\d+)/feedback$", path)
                if m:
                    try:
                        session.set_feedback(conn, user, int(m.group(1)), int(data.get("rating") or 0), str(data.get("note") or ""))
                    except (session.SessionError, ValueError) as exc:
                        raise ApiError(400, str(exc)) from exc
                    return self._json(200, {"id": int(m.group(1))})
                if path == "/api/query":
                    if not isinstance(data.get("query"), dict):
                        raise ApiError(400, "query must be an object")
                    return self._json(200, orch.run_query(data["query"]))
                m = re.match(r"^/api/anomalies/([0-9a-f]{16})$", path)
                if m:
                    try:
                        anomalies.set_status(conn, m.group(1), str(data.get("status", "")), str(data.get("note", "")))
                    except ValueError as exc:
                        raise ApiError(400, str(exc)) from exc
                    orch.audit.record("anomaly.status", {"id": m.group(1), "status": data.get("status")})
                    return self._json(200, {"id": m.group(1), "status": data.get("status")})
                m = re.match(r"^/api/optimize/([0-9a-f]{16})/implemented$", path)
                if m:
                    try:
                        result = verify.mark_implemented(conn, m.group(1), str(data.get("implemented_on", "")), str(data.get("note", "")))
                    except ValueError as exc:
                        raise ApiError(400, str(exc)) from exc
                    orch.audit.record("optimize.implemented", result)
                    return self._json(201, result)
                m = re.match(r"^/api/optimize/([0-9a-f]{16})$", path)
                if m:
                    try:
                        optimize.set_status(conn, m.group(1), str(data.get("status", "")), str(data.get("note", "")))
                    except ValueError as exc:
                        raise ApiError(400, str(exc)) from exc
                    orch.audit.record("optimize.status", {"id": m.group(1), "status": data.get("status")})
                    return self._json(200, {"id": m.group(1), "status": data.get("status")})
                m = re.match(r"^/api/govern/guardrails/([0-9a-f]{16})$", path)
                if m:
                    try:
                        result = govern.set_guardrail_status(conn, m.group(1), str(data.get("status", "")), str(data.get("note", "")))
                    except ValueError as exc:
                        raise ApiError(400, str(exc)) from exc
                    orch.audit.record("govern.guardrail", {"id": m.group(1), "status": result["status"]})
                    return self._json(200, result)
                m = re.match(r"^/api/govern/violations/([0-9a-f]{16})/exception$", path)
                if m:
                    try:
                        result = govern.add_exception(conn, m.group(1), str(data.get("owner", "")), str(data.get("reason", "")),
                                                      str(data.get("expires_on", "")))
                    except ValueError as exc:
                        raise ApiError(400, str(exc)) from exc
                    orch.audit.record("govern.exception", {"id": m.group(1), "expires_on": result["expires_on"]})
                    return self._json(201, result)
                m = re.match(r"^/api/govern/policies/([a-z0-9-]{1,63})$", path)
                if m:
                    if not isinstance(data.get("enabled"), bool):
                        raise ApiError(400, "enabled must be true or false")
                    try:
                        govern.set_policy_enabled(conn, m.group(1), data["enabled"])
                    except ValueError as exc:
                        raise ApiError(404, str(exc)) from exc
                    orch.audit.record("govern.policy", {"id": m.group(1), "enabled": data["enabled"]})
                    return self._json(200, {"id": m.group(1), "enabled": data["enabled"]})
                if path == "/api/budgets/parse":
                    text = data.get("text")
                    if not isinstance(text, str):
                        raise ApiError(400, "text must be a string")
                    return self._json(200, budgets.parse_budget(text, orch.vocabulary(), engine.reference_date(conn)))
                if path == "/api/budgets":
                    try:
                        spec = budgets.BudgetSpec.from_dict(data.get("spec"))
                        bid = budgets.create(conn, spec, str(data.get("source_text") or "")[:600])
                    except budgets.BudgetError as exc:
                        raise ApiError(400, str(exc)) from exc
                    handled = [h for h in budgets.evaluate(conn) if h["budget_id"] == bid]
                    return self._json(201, {"id": bid, "notifications": handled})
                if path == "/api/budgets/evaluate":
                    return self._json(200, {"notifications": budgets.evaluate(conn)})
                if path == "/api/pins":
                    title = data.get("title")
                    if not isinstance(title, str) or not 0 < len(title.strip()) <= 120:
                        raise ApiError(400, "title must be 1-120 characters")
                    q = CostQuery.from_dict(data.get("query") or {})
                    cur = conn.execute("INSERT INTO pins(title, query_json, created_at) VALUES (?,?,?)",
                                       (title.strip(), json.dumps(q.to_dict()), utc_now()))
                    orch.audit.record("pin.created", {"id": cur.lastrowid, "title": title.strip()})
                    return self._json(201, {"id": cur.lastrowid})
                raise ApiError(404, "Not found")
            self._dispatch(route)

        def do_DELETE(self):
            path = urlparse(self.path).path

            def route(conn):
                self._body()  # enforces JSON content type + same origin
                user = self._user()
                if cloud_app.enabled() and any(path.startswith(prefix) for prefix in ("/api/optimize/","/api/govern/","/api/anomalies/","/api/budgets")):
                    raise ApiError(501,"This workflow is not connected to the cloud backend yet.")
                for pattern, remove, cast in ((r"^/api/memories/(\d+)$", session.delete_memory, int),
                                              (r"^/api/reports/(\d+)$", session.delete_report, int),
                                              (r"^/api/conversations/([0-9a-f]{16})$", session.delete_conversation, str)):
                    ms = re.match(pattern, path)
                    if ms:
                        if not remove(conn, user, cast(ms.group(1))):
                            raise ApiError(404, "Not found")
                        return self._json(200, {"deleted": ms.group(1)})
                mi = re.match(r"^/api/optimize/([0-9a-f]{16})/implemented$", path)
                if mi:
                    if not verify.undo(conn, mi.group(1)):
                        raise ApiError(404, "No recorded change for this finding")
                    Orchestrator(conn).audit.record("optimize.implemented_undone", {"id": mi.group(1)})
                    return self._json(200, {"undone": mi.group(1)})
                mb = re.match(r"^/api/budgets/(\d+)$", path)
                if mb:
                    if not budgets.delete(conn, int(mb.group(1))):
                        raise ApiError(404, "Budget not found")
                    return self._json(200, {"deleted": int(mb.group(1))})
                m = re.match(r"^/api/pins/(\d+)$", path)
                if not m:
                    raise ApiError(404, "Not found")
                cur = conn.execute("DELETE FROM pins WHERE id=?", (int(m.group(1)),))
                if cur.rowcount == 0:
                    raise ApiError(404, "Pin not found")
                Orchestrator(conn).audit.record("pin.deleted", {"id": int(m.group(1))})
                return self._json(200, {"deleted": int(m.group(1))})
            self._dispatch(route)

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765, source_label: str = "local folder") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, source_label))
    httpd.daemon_threads = True
    return httpd
