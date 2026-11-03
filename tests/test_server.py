import http.client
import json
import threading

import pytest

from costlens.server import serve


@pytest.fixture()
def server(demo_db_path):
    httpd = serve(str(demo_db_path), "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    out = json.loads(raw) if r.getheader("Content-Type", "").startswith("application/json") else raw
    return r.status, out, r


def test_status_and_headers(server):
    s, body, r = call(server, "GET", "/api/status")
    assert s == 200 and body["synthetic"] is True and body["model"]["mode"] == "deterministic"
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    assert r.getheader("X-Content-Type-Options") == "nosniff"
    assert body["permissions"]["aws_access"] == "none"


def test_index_and_static(server):
    s, body, _ = call(server, "GET", "/")
    assert s == 200 and b"CostLens" in body
    assert call(server, "GET", "/static/app.js")[0] == 200
    assert call(server, "GET", "/static/../server.py")[0] == 404


def test_ask_flow(server):
    s, body, _ = call(server, "POST", "/api/ask", {"question": "Show cost by service for last month"})
    assert s == 200 and body["result"]["status"] == "ok" and body["worker"] == "visualization"
    prev = body["result"]["query"]
    s, body2, _ = call(server, "POST", "/api/ask", {"question": "now by account", "previous": prev})
    assert body2["result"]["query"]["group_by"] == "account"


def test_query_validation_error(server):
    s, body, _ = call(server, "POST", "/api/query", {"query": {"start": "2026-10-01", "end": "2026-09-01"}})
    assert s == 400 and "end date" in body["error"]


def test_host_header_enforced(server):
    s, _, _ = call(server, "GET", "/api/status", headers={"Host": "evil.example:80"})
    assert s == 421


def test_post_requires_json_and_same_origin(server):
    c = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    c.request("POST", "/api/ask", body=b"question=x", headers={"Host": f"127.0.0.1:{server}",
                                                                "Content-Type": "application/x-www-form-urlencoded"})
    assert c.getresponse().status == 415
    s, _, _ = call(server, "POST", "/api/ask", {"question": "x"}, headers={"Origin": "http://evil.example"})
    assert s == 403


def test_body_limit(server):
    s, _, _ = call(server, "POST", "/api/ask", {"question": "x" * 70000})
    assert s == 413


def test_pins_crud(server):
    q = {"start": "2026-10-01", "end": "2026-11-01", "group_by": "service"}
    s, body, _ = call(server, "POST", "/api/pins", {"title": "Oct by service", "query": q})
    assert s == 201
    pid = body["id"]
    s, body, _ = call(server, "GET", "/api/pins")
    assert [p["id"] for p in body["pins"]] == [pid]
    assert call(server, "DELETE", f"/api/pins/{pid}", {})[0] == 200
    assert call(server, "DELETE", f"/api/pins/{pid}", {})[0] == 404
    assert call(server, "POST", "/api/pins", {"title": "", "query": q})[0] == 400


def test_audit_records_and_redacts(server):
    call(server, "POST", "/api/ask", {"question": "cost by service AKIAABCDEFGHIJKLMNOP last month"})
    s, body, _ = call(server, "GET", "/api/audit")
    q_events = [e for e in body["events"] if e["event_type"] == "question.received"]
    assert q_events and "AKIA" not in q_events[0]["detail"] and "[REDACTED]" in q_events[0]["detail"]
    assert any(e["event_type"] == "tool.call" for e in body["events"])
