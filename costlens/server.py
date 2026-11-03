"""Local HTTP server (Python standard library only).

Binds to 127.0.0.1 by default. Defences for a local tool:
  * Host header must be localhost/127.0.0.1/[::1] (blocks DNS-rebinding).
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
from urllib.parse import urlparse

from .agent.orchestrator import Orchestrator, PLANNED_WORKERS, model_mode
from .query import engine
from .query.model import CostQuery, QueryError
from .status import data_status
from .store import connect, utc_now

WEB_DIR = Path(__file__).parent / "web"
STATIC = {"app.js": "text/javascript", "styles.css": "text/css", "charts.js": "text/javascript"}
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
    class Handler(BaseHTTPRequestHandler):
        server_version = "CostLens"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep request logs free of bodies
            log.info("%s %s", self.command, self.path.split("?")[0])

        # ---------------------------------------------------------- plumbing
        def _headers(self, status: int, ctype: str, length: int, cache: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-cache" if cache else "no-store")
            self.end_headers()

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload, default=str).encode()
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _check_host(self):
            if not _HOST_RE.match(self.headers.get("Host", "")):
                raise ApiError(421, "Requests must use localhost.")

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

        def _dispatch(self, fn):
            conn = None
            try:
                self._check_host()
                conn = connect(db_path)
                fn(conn)
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except QueryError as exc:
                self._json(400, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                log.error("unhandled error: %s", traceback.format_exc(limit=3))
                self._json(500, {"error": "Internal error. Check the server log."})
            finally:
                if conn is not None:
                    conn.close()

        # ---------------------------------------------------------- routes
        def do_GET(self):
            path = urlparse(self.path).path
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
                if path == "/api/status":
                    s = data_status(conn, source_label)
                    s["model"] = model_mode()
                    s["planned_modules"] = PLANNED_WORKERS
                    return self._json(200, s)
                if path == "/api/dimensions":
                    return self._json(200, engine.dimension_values(conn))
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
                if path == "/api/ask":
                    q = data.get("question")
                    if not isinstance(q, str):
                        raise ApiError(400, "question must be a string")
                    prev = data.get("previous")
                    return self._json(200, orch.ask(q, prev if isinstance(prev, dict) else None))
                if path == "/api/query":
                    if not isinstance(data.get("query"), dict):
                        raise ApiError(400, "query must be an object")
                    return self._json(200, orch.run_query(data["query"]))
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
