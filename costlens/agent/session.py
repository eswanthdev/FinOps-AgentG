"""What the agent keeps between questions: conversations, memory, run traces and reports.

Every read and write is scoped to one user, except the run summary, which is an operator view.
Rules:
  * Earlier turns reach the model as plain text. Numbers in them are not evidence, so the model
    must fetch them again, and the number check still applies to every answer.
  * Memory is written only by the person, never by the model. It holds preferences and facts, not
    money: amounts are refused, because a remembered number would go stale.
  * Memory and history are data. They are passed as quoted text, never as instructions.
  * Reports keep their queries, so opening one shows current figures beside the saved answer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from ..query import engine
from ..query.model import CostQuery, QueryError
from ..store import utc_now
from .orchestrator import redact

MAX_HISTORY_MESSAGES = 16
MAX_HISTORY_CHARS = 12_000
MAX_MEMORIES = 50
MAX_MEMORY_CHARS = 300
MAX_REPORTS = 200
DEFAULT_MONTHLY_TOKENS = 20_000_000
DEFAULT_RETENTION_DAYS = 90
_REMEMBER_RE = re.compile(r"^\s*(?:please\s+)?remember(?:\s+that)?\s*[:,]?\s+(.+)$", re.I | re.S)
_MONEY_RE = re.compile(r"[$€£₹]|\b\d[\d,.]*\s*(?:usd|eur|inr|dollars?|rupees?)\b", re.I)
log = logging.getLogger("costlens.agent")


class SessionError(ValueError):
    pass


# ------------------------------------------------------------------ conversations

def ensure_conversation(conn, user_id: str, conversation_id: str | None, first_text: str) -> str:
    if conversation_id:
        row = conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).fetchone()
        if row:
            return row["id"]
    now = utc_now()
    new_id = hashlib.sha256(f"{user_id}|{now}|{os.urandom(8).hex()}".encode()).hexdigest()[:16]
    conn.execute("INSERT INTO conversations(id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                 (new_id, user_id, redact(first_text.strip())[:80] or "New conversation", now, now))
    return new_id


def add_message(conn, conversation_id: str, role: str, text: str, run_id: int | None = None) -> None:
    now = utc_now()
    conn.execute("INSERT INTO conversation_messages(conversation_id, role, text, run_id, created_at) VALUES (?,?,?,?,?)",
                 (conversation_id, role, redact(text)[:8000], run_id, now))
    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))


def history(conn, user_id: str, conversation_id: str | None) -> list[dict]:
    """The recent turns, oldest first, within the size limits. Always starts with a user turn."""
    if not conversation_id:
        return []
    rows = conn.execute(
        "SELECT m.role, m.text FROM conversation_messages m JOIN conversations c ON c.id=m.conversation_id "
        "WHERE c.id=? AND c.user_id=? ORDER BY m.id DESC LIMIT ?", (conversation_id, user_id, MAX_HISTORY_MESSAGES)).fetchall()
    kept, size = [], 0
    for r in rows:
        size += len(r["text"])
        if size > MAX_HISTORY_CHARS:
            break
        kept.append({"role": r["role"], "content": r["text"]})
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


def list_conversations(conn, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC, id LIMIT 100", (user_id,))]


def get_conversation(conn, user_id: str, conversation_id: str) -> dict | None:
    c = conn.execute("SELECT id, title, created_at, updated_at FROM conversations WHERE id=? AND user_id=?",
                     (conversation_id, user_id)).fetchone()
    if not c:
        return None
    rows = conn.execute("SELECT role, text, run_id, created_at FROM conversation_messages WHERE conversation_id=? ORDER BY id", (conversation_id,))
    return {**dict(c), "messages": [dict(r) for r in rows]}


def delete_conversation(conn, user_id: str, conversation_id: str) -> bool:
    return conn.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)).rowcount > 0


# ------------------------------------------------------------------ memory

def remember_request(text: str) -> str | None:
    m = _REMEMBER_RE.match(text)
    return m.group(1).strip() if m else None


def add_memory(conn, user_id: str, text: str) -> dict:
    text = " ".join(text.split())
    if not 0 < len(text) <= MAX_MEMORY_CHARS:
        raise SessionError(f"a memory must be 1-{MAX_MEMORY_CHARS} characters")
    if _MONEY_RE.search(text):
        raise SessionError("memory cannot hold amounts, because they go stale. Ask for the figure instead.")
    if redact(text) != text:
        raise SessionError("this looks like a secret, so it was not saved")
    if conn.execute("SELECT COUNT(*) FROM memories WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_MEMORIES:
        raise SessionError(f"the limit is {MAX_MEMORIES} memories. Delete one first.")
    cur = conn.execute("INSERT INTO memories(user_id, text, created_at) VALUES (?,?,?)", (user_id, text, utc_now()))
    return {"id": cur.lastrowid, "text": text}


def list_memories(conn, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, text, created_at FROM memories WHERE user_id=? ORDER BY id", (user_id,))]


def delete_memory(conn, user_id: str, memory_id: int) -> bool:
    return conn.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)).rowcount > 0


# ------------------------------------------------------------------ run traces

def monthly_token_cap() -> int:
    value = os.environ.get("COSTLENS_AGENT_MONTHLY_TOKENS", "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else DEFAULT_MONTHLY_TOKENS


def tokens_this_month(conn) -> int:
    month = utc_now()[:7]
    row = conn.execute("SELECT COALESCE(SUM(input_tokens + output_tokens),0) AS t FROM agent_runs WHERE substr(started_at,1,7)=?", (month,)).fetchone()
    return row["t"]


def record_run(conn, user_id: str, conversation_id: str | None, model: str, question: str, result: dict, duration_ms: int) -> int:
    trace = result.get("trace", [])
    status = "ok" if result["status"] == "ok" else result.get("reason", "failed")
    cur = conn.execute(
        "INSERT INTO agent_runs(user_id, conversation_id, started_at, duration_ms, status, note, model, turns, tool_calls, input_tokens, "
        "output_tokens, repaired, question, answer, trace_json, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, conversation_id, utc_now(), duration_ms, status, result.get("note", ""), model, result.get("turns", 0), len(trace),
         result.get("input_tokens", 0), result.get("output_tokens", 0), 1 if result.get("repaired") else 0, redact(question)[:2000],
         result.get("answer", ""), json.dumps(trace), json.dumps(result.get("evidence_queries", []))))
    log.info(json.dumps({"event": "agent_run", "run_id": cur.lastrowid, "status": status, "model": model, "turns": result.get("turns", 0),
                         "tool_calls": len(trace), "input_tokens": result.get("input_tokens", 0), "output_tokens": result.get("output_tokens", 0),
                         "duration_ms": duration_ms, "repaired": bool(result.get("repaired"))}))
    if status != "ok":
        log.warning("agent_run_failed %s", status)  # the CloudWatch alarm counts this word
    return cur.lastrowid


def set_feedback(conn, user_id: str, run_id: int, rating: int, note: str = "") -> None:
    if rating not in (1, -1):
        raise SessionError("rating must be 1 or -1")
    if conn.execute("UPDATE agent_runs SET rating=?, feedback=? WHERE id=? AND user_id=?", (rating, note[:500], run_id, user_id)).rowcount == 0:
        raise SessionError("run not found")


def runs_summary(conn, limit: int = 50) -> dict:
    """Operator view for the Agent runs page. It holds no answers from other users."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
    t = conn.execute(
        "SELECT COUNT(*) AS runs, COALESCE(SUM(status='ok'),0) AS ok, COALESCE(SUM(repaired),0) AS repaired, COALESCE(SUM(input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(rating=1),0) AS thumbs_up, COALESCE(SUM(rating=-1),0) AS thumbs_down "
        "FROM agent_runs WHERE started_at >= ?", (since,)).fetchone()
    durations = sorted(r["duration_ms"] for r in conn.execute("SELECT duration_ms FROM agent_runs WHERE started_at >= ?", (since,)))
    runs = [dict(r) for r in conn.execute(
        "SELECT id, started_at, duration_ms, status, note, model, turns, tool_calls, input_tokens, output_tokens, repaired, rating, trace_json "
        "FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,))]
    for r in runs:
        r["tools"] = [s["tool"] for s in json.loads(r.pop("trace_json"))]
    return {"last_30_days": {**dict(t), "failed": t["runs"] - t["ok"], "median_ms": durations[len(durations) // 2] if durations else None},
            "tokens_this_month": tokens_this_month(conn), "monthly_token_cap": monthly_token_cap(), "runs": runs,
            "cost_note": "Tokens are measured. Dollar cost is not shown because no verified model price is configured."}


# ------------------------------------------------------------------ reports

def save_report(conn, user_id: str, run_id: int, title: str = "") -> int:
    run = conn.execute("SELECT * FROM agent_runs WHERE id=? AND user_id=? AND status='ok'", (run_id, user_id)).fetchone()
    if not run:
        raise SessionError("only a finished answer of yours can be saved as a report")
    if conn.execute("SELECT COUNT(*) FROM reports WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_REPORTS:
        raise SessionError(f"the limit is {MAX_REPORTS} reports")
    name = (title.strip() or run["question"])[:120]
    return conn.execute("INSERT INTO reports(user_id, title, question, answer, trace_json, evidence_json, created_at) VALUES (?,?,?,?,?,?,?)",
                        (user_id, name, run["question"], run["answer"], run["trace_json"], run["evidence_json"], utc_now())).lastrowid


def list_reports(conn, user_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, title, question, created_at FROM reports WHERE user_id=? ORDER BY id DESC", (user_id,))]


def get_report(conn, user_id: str, report_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM reports WHERE id=? AND user_id=?", (report_id, user_id)).fetchone()
    if not row:
        return None
    current = []
    for e in json.loads(row["evidence_json"]):
        try:
            from .. import cloud_app
            result = cloud_app.query(e["query"]) if cloud_app.enabled() else engine.run(conn, CostQuery.from_dict(e["query"]))
            current.append({"title": e["title"], "query": e["query"], "status": result.get("status"),
                            "summary": result.get("summary") or result.get("reason", "")})
        except QueryError as exc:
            current.append({"title": e["title"], "query": e["query"], "status": "error", "summary": str(exc)})
    out = {k: row[k] for k in ("id", "title", "question", "answer", "created_at")}
    return {**out, "trace": json.loads(row["trace_json"]), "current": current,
            "note": "The saved answer is from the date shown. Current figures are recalculated from CUR now."}


def delete_report(conn, user_id: str, report_id: int) -> bool:
    return conn.execute("DELETE FROM reports WHERE id=? AND user_id=?", (report_id, user_id)).rowcount > 0


def purge(conn, days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete chats and traces past the retention window. Reports are kept until a person deletes them."""
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    removed = conn.execute("DELETE FROM conversations WHERE updated_at < ?", (cutoff,)).rowcount
    return removed + conn.execute("DELETE FROM agent_runs WHERE started_at < ?", (cutoff,)).rowcount
