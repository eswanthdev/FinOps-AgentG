"""Budgets, forecasts and budget alerts.

These are CostLens budgets stored in the local database. They are NOT AWS Budgets
and nothing is created in AWS.

Forecast method (simple, transparent run-rate; an estimate, not a prediction):
  daily rate   = average daily cost of the last 7 days with data (same scope)
  forecast     = actual so far in the period + daily rate x days left in the period
  range        = actual + (lowest day of last 7) x days left  ...  actual + (highest day) x days left
  confidence   = "low" if fewer than 14 days of recent history, or more than 21 days left;
                 "medium" otherwise; "high" only when 7 days or fewer are left with 14+ days history.
Known limits: no seasonality, no planned changes, monthly one-off charges (e.g. tax,
upfront fees) are not projected.

Alerts: each threshold (e.g. 80% of actual, 100% of forecast) fires at most once per
budget period. Delivery goes through notify.get_notifier().
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .agent.interpreter import DeterministicInterpreter, Vocabulary
from .money import NANOS_PER_USD, nanos_to_cents_str, parse_usd_to_nanos
from .notify import get_notifier, valid_email
from .query import engine
from .query.engine import _fmt_usd, _where, account_names, label_for
from .query.model import CostQuery, QueryError, is_dimension
from .store import get_meta, utc_now

PERIODS = ("monthly", "quarterly", "yearly")
THRESHOLD_TYPES = ("actual", "forecasted")
DEFAULT_THRESHOLDS = [(80, "actual"), (100, "actual"), (100, "forecasted")]
MAX_AMOUNT_USD = 1_000_000_000


class BudgetError(ValueError):
    pass


@dataclass
class Threshold:
    pct: int
    type: str = "actual"

    @property
    def key(self) -> str:
        return f"{self.type}-{self.pct}"


@dataclass
class BudgetSpec:
    name: str
    amount: str  # USD decimal string, e.g. "5000.00"
    period: str = "monthly"
    metric: str = "unblended"
    filters: dict[str, list[str]] = field(default_factory=dict)
    thresholds: list[Threshold] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)

    def validate(self) -> "BudgetSpec":
        self.name = re.sub(r"\s+", " ", (self.name or "").strip())
        if not 1 <= len(self.name) <= 80:
            raise BudgetError("Budget name must be 1 to 80 characters.")
        try:
            nanos = parse_usd_to_nanos(self.amount)
        except ValueError as exc:
            raise BudgetError("Budget amount must be a number in USD.") from exc
        if nanos <= 0 or nanos > MAX_AMOUNT_USD * NANOS_PER_USD:
            raise BudgetError("Budget amount must be more than $0.")
        self.amount = nanos_to_cents_str(nanos)
        if self.period not in PERIODS:
            raise BudgetError(f"Period must be one of {', '.join(PERIODS)}.")
        if self.metric not in ("unblended", "amortized"):
            raise BudgetError("Cost type must be unblended or amortized.")
        # Reuse CostQuery validation for the scope.
        try:
            CostQuery(start=date(2026, 1, 1), end=date(2026, 1, 2), filters=self.filters, group_by=None).validate()
        except QueryError as exc:
            raise BudgetError(f"Invalid scope: {exc}") from exc
        if not self.thresholds:
            raise BudgetError("At least one alert threshold is needed.")
        if len(self.thresholds) > 10:
            raise BudgetError("At most 10 alert thresholds.")
        seen = set()
        for t in self.thresholds:
            if t.type not in THRESHOLD_TYPES or not 1 <= int(t.pct) <= 500:
                raise BudgetError("Thresholds must be 1-500% of type actual or forecasted.")
            t.pct = int(t.pct)
            if t.key in seen:
                raise BudgetError(f"Duplicate threshold {t.key}.")
            seen.add(t.key)
        self.thresholds.sort(key=lambda t: (t.type != "actual", t.pct))
        self.recipients = list(dict.fromkeys(r.strip().lower() for r in self.recipients))
        if len(self.recipients) > 10:
            raise BudgetError("At most 10 email recipients.")
        bad = [r for r in self.recipients if not valid_email(r)]
        if bad:
            raise BudgetError(f"Not a valid email address: {bad[0]}")
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["thresholds"] = [{"pct": t.pct, "type": t.type} for t in self.thresholds]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetSpec":
        if not isinstance(d, dict):
            raise BudgetError("Budget must be an object.")
        allowed = {"name", "amount", "period", "metric", "filters", "thresholds", "recipients"}
        if set(d) - allowed:
            raise BudgetError(f"Unknown budget fields: {', '.join(sorted(set(d) - allowed))}")
        ths = d.get("thresholds") or []
        if not isinstance(ths, list) or not all(isinstance(t, dict) for t in ths):
            raise BudgetError("thresholds must be a list of {pct, type}.")
        try:
            thresholds = [Threshold(pct=int(t.get("pct")), type=str(t.get("type", "actual"))) for t in ths]
        except (TypeError, ValueError) as exc:
            raise BudgetError("Threshold pct must be a whole number.") from exc
        recips = d.get("recipients") or []
        if not isinstance(recips, list) or not all(isinstance(r, str) for r in recips):
            raise BudgetError("recipients must be a list of email addresses.")
        filters = d.get("filters") or {}
        if not isinstance(filters, dict):
            raise BudgetError("filters must be an object.")
        return cls(name=str(d.get("name", "")), amount=str(d.get("amount", "")), period=str(d.get("period", "monthly")),
                   metric=str(d.get("metric", "unblended")), filters=filters, thresholds=thresholds,
                   recipients=recips).validate()


# ----------------------------------------------------------------- periods

def period_window(period: str, ref: date) -> tuple[date, date]:
    """Calendar period containing `ref` (the day after data is complete). End exclusive."""
    d = ref - timedelta(days=1)  # last day we can have data for
    if period == "monthly":
        start = date(d.year, d.month, 1)
        end = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
    elif period == "quarterly":
        qm = 3 * ((d.month - 1) // 3) + 1
        start = date(d.year, qm, 1)
        end = date(d.year + (qm == 10), 1 if qm == 10 else qm + 3, 1)
    else:
        start, end = date(d.year, 1, 1), date(d.year + 1, 1, 1)
    return start, end


def period_label(period: str, start: date) -> str:
    if period == "monthly":
        return start.strftime("%B %Y")
    if period == "quarterly":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    return str(start.year)


# ----------------------------------------------------------------- forecast

def _daily(conn, filters: dict, metric: str, start: date, end: date) -> dict[str, int]:
    q = CostQuery(start=start, end=end, filters=filters, group_by=None, metric=metric)
    where, params = _where(q, start, end)
    col = "unblended_nanos" if metric == "unblended" else "amortized_nanos"
    return {r["d"]: r["t"] for r in conn.execute(
        f"SELECT usage_date AS d, SUM({col}) AS t FROM line_items WHERE {where} GROUP BY d", params)}


def forecast(conn, filters: dict, metric: str, start: date, end: date) -> dict:
    """Run-rate forecast for [start, end). See module docstring for the method."""
    cov = engine.coverage(conn, start, end)
    all_days = [r[0] for r in conn.execute("SELECT DISTINCT usage_date FROM line_items ORDER BY 1")]
    if not all_days:
        return {"status": "insufficient_data", "reason": "No billing data ingested yet."}
    data_through = date.fromisoformat(all_days[-1])
    recent = [d for d in all_days if d <= data_through.isoformat()][-7:]
    history_30 = [d for d in all_days if d > (data_through - timedelta(days=30)).isoformat()]
    if len(recent) < 3:
        return {"status": "insufficient_data", "reason": "Fewer than 3 days of recent data; no forecast."}
    if metric == "amortized":
        q = CostQuery(start=start, end=end, filters=filters, group_by=None)
        w, p = _where(q, date.fromisoformat(recent[0]), end)
        if conn.execute(f"SELECT 1 FROM line_items WHERE {w} AND amortized_nanos IS NULL LIMIT 1", p).fetchone():
            return {"status": "insufficient_data", "reason": "Some charges cannot be amortized in this release."}

    actual_end = min(end, data_through + timedelta(days=1))
    actual_days = _daily(conn, filters, metric, start, actual_end) if actual_end > start else {}
    actual = sum(actual_days.values())
    rec = _daily(conn, filters, metric, date.fromisoformat(recent[0]), data_through + timedelta(days=1))
    vals = [rec.get(d, 0) for d in recent]
    rate = sum(vals) // len(vals)
    first_open = max(start, data_through + timedelta(days=1))
    days_left = max(0, (end - first_open).days)
    fc = actual + rate * days_left
    low = actual + min(vals) * days_left
    high = actual + max(vals) * days_left
    if len(history_30) < 14 or days_left > 21:
        confidence = "low"
    elif days_left <= 7:
        confidence = "high"
    else:
        confidence = "medium"
    notes = list(cov["notes"])
    missing = [p["period"] for p in cov["periods"] if p["status"] == "missing"]
    if missing:
        notes.append("Missing billing data for " + ", ".join(missing) + ": actual spend is understated.")
    if days_left > 21:
        notes.append(f"{days_left} days are projected from {len(vals)} days of data: treat as a rough estimate.")
    return {
        "status": "ok",
        "period_start": start.isoformat(), "period_end": end.isoformat(),
        "data_through": data_through.isoformat(),
        "actual": nanos_to_cents_str(actual), "actual_display": _fmt_usd(actual), "actual_nanos": actual,
        "daily_rate_display": _fmt_usd(rate),
        "days_left": days_left, "days_elapsed": len(actual_days),
        "forecast": nanos_to_cents_str(fc), "forecast_display": _fmt_usd(fc), "forecast_nanos": fc,
        "low_display": _fmt_usd(low), "high_display": _fmt_usd(high),
        "confidence": confidence,
        "method": f"Actual to date + average of the last {len(vals)} days ({_fmt_usd(rate)}/day) x {days_left} days left. "
                  "Range uses the lowest and highest of those days. Estimate only.",
        "rate_window": [recent[0], recent[-1]],
        "notes": notes,
    }


# ----------------------------------------------------------------- parsing

_AMOUNT_CUR = re.compile(
    r"(?:\$|usd\s*|us\$\s*)([\d][\d,]*(?:\.\d+)?)\s*(k|m|thousand|million)?\b|"
    r"\b([\d][\d,]*(?:\.\d+)?)\s*(k|m|thousand|million)?\s*(?:usd|dollars?|\$)", re.I)
_AMOUNT_BARE = re.compile(r"\b(?:budget|limit|cap|of)\s+(?:of\s+)?([\d][\d,]*(?:\.\d+)?)\s*(k|m|thousand|million)?\b(?!\s*%)", re.I)
_EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_MULT = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}


def _amount(text: str) -> tuple[str | None, bool]:
    for rx, bare in ((_AMOUNT_CUR, False), (_AMOUNT_BARE, True)):
        m = rx.search(text)
        if m:
            g = [x for x in m.groups()]
            num = g[0] if not bare and g[0] else (g[2] if not bare else g[0])
            unit = (g[1] if not bare and g[0] else (g[3] if not bare else g[1])) or ""
            from decimal import Decimal
            v = Decimal(num.replace(",", "")) * _MULT.get(unit.lower(), 1)
            return str(v), bare
    return None, False


def is_budget_request(text: str) -> bool:
    t = text.lower()
    return bool(re.search(r"\b(set ?up|create|add|make|new|define|configure)\b.*\bbudgets?\b|\bbudgets?\b.*\b(of|for)\b.*(\$|\d)",
                          t)) and not re.search(r"\b(list|show|what are|which)\b.*\bbudgets\b", t)


def parse_budget(text: str, vocab: Vocabulary, today: date) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {"status": "clarify", "message": "Describe the budget, for example: "
                "“Set up a monthly budget of $5,000 for prod-data and alert ops@example.com at 80% and 100%.”"}
    if len(raw) > 600:
        return {"status": "clarify", "message": "Please keep the budget description under 600 characters."}
    low = raw.lower()
    if re.search(r"₹|\binr\b|\brupees?\b|\blakhs?\b|\bcrores?\b|€|£|\beur\b|\bgbp\b", low):
        return {"status": "clarify", "message": "Budgets are in US dollars in this release, because the AWS bill "
                "data is in USD. Please give the amount in USD, for example $5,000."}
    understood, assumptions = [], []
    recipients = list(dict.fromkeys(e.lower() for e in _EMAIL_FIND.findall(raw)))
    text = _EMAIL_FIND.sub(" ", low)

    amount, bare = _amount(text)
    if amount is None:
        return {"status": "clarify", "message": "I could not find the budget amount. Please include it in USD, "
                "for example “a budget of $5,000”."}
    if bare:
        assumptions.append("No currency given: treated the amount as USD.")
    text_wo_amount = _AMOUNT_CUR.sub(" ", text)

    if re.search(r"\b(quarter|quarterly)\b", text):
        period = "quarterly"
    elif re.search(r"\b(year|yearly|annual|annually)\b", text):
        period = "yearly"
    else:
        period = "monthly"
        if not re.search(r"\b(month|monthly)\b", text):
            assumptions.append("No period given: made it a monthly budget.")
    understood.append(f"{period} budget")

    filters = DeterministicInterpreter()._filters(text_wo_amount, vocab, understood)
    if not filters:
        assumptions.append("No account, service or team given: the budget covers all spend.")
    metric = "amortized" if re.search(r"\bamorti[sz]ed\b", text) else "unblended"

    thresholds: list[Threshold] = []
    for m in re.finditer(r"(\d{1,3})\s*%", text):
        before = text[max(0, m.start() - 45):m.start()]
        kind = "forecasted" if re.search(r"forecast|projected|predicted|expected to", before) and \
            not re.search(r"actual", before[before.rfind("forecast") if "forecast" in before else 0:]) else "actual"
        t = Threshold(int(m.group(1)), kind)
        if t.key not in {x.key for x in thresholds}:
            thresholds.append(t)
    if not thresholds and re.search(r"\b(forecast|projected)\b.*\b(exceed|over|above|cross)", text):
        thresholds.append(Threshold(100, "forecasted"))
    if not thresholds:
        thresholds = [Threshold(p, k) for p, k in DEFAULT_THRESHOLDS]
        assumptions.append("No alert levels given: alerts at 80% and 100% of actual spend, and 100% of forecast.")
    if not recipients:
        assumptions.append("No email address given: alerts will show in the app but no one will be emailed. "
                           "Add an address to send alerts.")

    m = re.search(r"\b(?:named|called|name it)\s+[\"“']?([^\"”',.]{1,60})", raw, re.I)
    if m:
        name = m.group(1).strip()
    else:
        names = account_names_from_vocab(vocab)
        parts = []
        for dim, vals in filters.items():
            shown = ", ".join(names.get(v, v) if dim == "account" else ("untagged" if v == "(untagged)" else v)
                              for v in vals)
            if dim.startswith("tag:"):
                shown = f"{dim[4:].replace('user:', '')} {shown}"
            parts.append(shown)
        label = " · ".join(parts) or "All spend"
        name = label[:1].upper() + label[1:] + f" – {period}"
    spec = BudgetSpec(name=name[:80], amount=amount, period=period, metric=metric, filters=filters,
                      thresholds=thresholds, recipients=recipients)
    try:
        spec.validate()
    except BudgetError as exc:
        return {"status": "clarify", "message": str(exc)}
    understood.insert(0, f"amount {_fmt_usd(parse_usd_to_nanos(spec.amount))}")
    if recipients:
        understood.append("email " + ", ".join(recipients))
    names = account_names_from_vocab(vocab)
    scope = [{"dimension": d, "values": [f"{names[v]} ({v})" if d == "account" and v in names else v for v in vals]}
             for d, vals in spec.filters.items()]
    return {"status": "ok", "spec": spec.to_dict(), "scope": scope, "understood": understood,
            "assumptions": assumptions, "source_text": raw}


def account_names_from_vocab(vocab: Vocabulary) -> dict[str, str]:
    return dict(vocab.accounts)


# ----------------------------------------------------------------- store + evaluate

def create(conn, spec: BudgetSpec, source_text: str = "") -> int:
    spec.validate()
    if conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] >= 200:
        raise BudgetError("Budget limit reached (200).")
    cur = conn.execute("INSERT INTO budgets(name, spec_json, source_text, created_at) VALUES (?,?,?,?)",
                       (spec.name, json.dumps(spec.to_dict()), source_text[:600], utc_now()))
    conn.execute("INSERT INTO audit_events(ts, event_type, detail) VALUES (?,?,?)",
                 (utc_now(), "budget.created", json.dumps({"id": cur.lastrowid, "name": spec.name})))
    return cur.lastrowid


def delete(conn, budget_id: int) -> bool:
    n = conn.execute("DELETE FROM budgets WHERE id=?", (budget_id,)).rowcount
    if n:
        conn.execute("INSERT INTO audit_events(ts, event_type, detail) VALUES (?,?,?)",
                     (utc_now(), "budget.deleted", json.dumps({"id": budget_id})))
    return bool(n)


def status_for(conn, row) -> dict:
    spec = BudgetSpec.from_dict(json.loads(row["spec_json"]))
    ref = engine.reference_date(conn)
    start, end = period_window(spec.period, ref)
    amount = parse_usd_to_nanos(spec.amount)
    fc = forecast(conn, spec.filters, spec.metric, start, end)
    names = account_names(conn)
    scope = [{"dimension": k, "values": [label_for(k, v, names) if k == "account" else v for v in vals]}
             for k, vals in spec.filters.items()]
    out = {"id": row["id"], "name": spec.name, "spec": spec.to_dict(), "source_text": row["source_text"],
           "created_at": row["created_at"], "amount_display": _fmt_usd(amount), "scope": scope,
           "period_label": period_label(spec.period, start), "period_start": start.isoformat(),
           "period_end": end.isoformat(), "forecast": fc}
    sent = {r["threshold_key"]: dict(r) for r in conn.execute(
        "SELECT threshold_key, delivery_status, created_at FROM notifications WHERE budget_id=? AND period_start=?",
        (row["id"], start.isoformat()))}
    alerts = []
    if fc["status"] == "ok":
        used_pct = fc["actual_nanos"] * 1000 // amount
        fc_pct = fc["forecast_nanos"] * 1000 // amount
        out["used_pct"] = f"{used_pct // 10}.{used_pct % 10}"
        out["forecast_pct"] = f"{fc_pct // 10}.{fc_pct % 10}"
        out["remaining_display"] = _fmt_usd(amount - fc["actual_nanos"])
    for t in spec.thresholds:
        level = amount * t.pct // 100
        value = fc.get("actual_nanos" if t.type == "actual" else "forecast_nanos") if fc["status"] == "ok" else None
        triggered = value is not None and value >= level
        n = sent.get(t.key)
        alerts.append({"key": t.key, "pct": t.pct, "type": t.type, "level_display": _fmt_usd(level),
                       "triggered": triggered, "notification": n})
    out["alerts"] = alerts
    if fc["status"] != "ok":
        out["state"] = "no_data"
    elif any(a["triggered"] and a["type"] == "actual" and a["pct"] >= 100 for a in alerts) or fc["actual_nanos"] >= amount:
        out["state"] = "exceeded"
    elif fc["forecast_nanos"] >= amount:
        out["state"] = "forecast_exceeded"
    elif any(a["triggered"] for a in alerts):
        out["state"] = "warning"
    else:
        out["state"] = "on_track"
    return out


def list_status(conn) -> list[dict]:
    return [status_for(conn, r) for r in conn.execute("SELECT * FROM budgets ORDER BY id DESC")]


def _message(conn, b: dict, a: dict) -> tuple[str, str]:
    synthetic = get_meta(conn, "data_mode") == "synthetic"
    fc = b["forecast"]
    what = "actual spend" if a["type"] == "actual" else "forecasted spend"
    value = fc["actual_display"] if a["type"] == "actual" else fc["forecast_display"]
    subject = (f"{'[SYNTHETIC DATA] ' if synthetic else ''}Budget alert: {b['name']} – {what} "
               f"reached {a['pct']}% of {b['amount_display']}")
    scope = "; ".join(f"{s['dimension'].replace('tag:user:', '')}: {', '.join(s['values'])}" for s in b["scope"]) or "all spend"
    body = "\n".join([
        f"Budget: {b['name']}",
        f"Scope: {scope}",
        f"Period: {b['period_label']} ({b['spec']['metric']} cost)",
        f"Budget amount: {b['amount_display']}",
        f"Alert: {what} at or above {a['pct']}% ({a['level_display']})",
        f"Actual so far: {fc['actual_display']} (data through {fc['data_through']})",
        f"Forecast for the period: {fc['forecast_display']} (range {fc['low_display']} – {fc['high_display']}, "
        f"{fc['confidence']} confidence; estimate only)",
        "",
        "Sent by CostLens. Forecasts are estimates, not invoices.",
        "THIS ALERT IS BASED ON SYNTHETIC DEMO DATA." if synthetic else "",
    ]).strip() + "\n"
    return subject, body


def evaluate(conn, notifier=None) -> list[dict]:
    """Check every budget; send each newly triggered alert once per period. Returns notifications handled."""
    notifier = notifier or get_notifier()
    handled = []
    for b in list_status(conn):
        recipients = b["spec"]["recipients"]
        for a in b["alerts"]:
            if not a["triggered"]:
                continue
            prev = a["notification"]
            if prev and prev["delivery_status"] == "sent":
                continue
            subject, body = _message(conn, b, a)
            if recipients:
                status, detail = notifier.send(recipients, subject, body)
            else:
                status, detail = "no_recipients", "No email recipients configured for this budget."
            conn.execute(
                "INSERT INTO notifications(budget_id, threshold_key, period_start, recipients, subject, body, "
                "delivery_status, delivery_detail, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(budget_id, threshold_key, period_start) DO UPDATE SET delivery_status=excluded.delivery_status, "
                "delivery_detail=excluded.delivery_detail, recipients=excluded.recipients, created_at=excluded.created_at",
                (b["id"], a["key"], b["period_start"], json.dumps(recipients), subject, body, status, detail, utc_now()))
            conn.execute("INSERT INTO audit_events(ts, event_type, detail) VALUES (?,?,?)",
                         (utc_now(), "budget.alert", json.dumps({"budget_id": b["id"], "threshold": a["key"],
                                                                  "status": status, "recipients": len(recipients)})))
            handled.append({"budget_id": b["id"], "budget": b["name"], "threshold": a["key"], "status": status,
                            "detail": detail, "recipients": recipients})
    return handled


def outbox(conn, limit: int = 100) -> list[dict]:
    out = []
    for r in conn.execute("SELECT n.*, b.name AS budget_name FROM notifications n LEFT JOIN budgets b ON b.id=n.budget_id "
                          "ORDER BY n.id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["recipients"] = json.loads(d["recipients"])
        out.append(d)
    return out


def org_forecast(conn) -> dict:
    ref = engine.reference_date(conn)
    start, end = period_window("monthly", ref)
    total = forecast(conn, {}, "unblended", start, end)
    by_account = []
    for acct, name in sorted(account_names(conn).items(), key=lambda x: x[1]):
        f = forecast(conn, {"account": [acct]}, "unblended", start, end)
        if f["status"] == "ok":
            by_account.append({"account": f"{name} ({acct})", "actual_display": f["actual_display"],
                               "forecast_display": f["forecast_display"], "low_display": f["low_display"],
                               "high_display": f["high_display"], "forecast_cents": int(f["forecast"].replace(".", ""))})
    by_account.sort(key=lambda x: -x["forecast_cents"])
    return {"period_label": period_label("monthly", start), "total": total, "by_account": by_account}
