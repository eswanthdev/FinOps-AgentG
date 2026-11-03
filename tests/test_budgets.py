from datetime import date

import pytest

from costlens import budgets, notify
from costlens.agent.orchestrator import Orchestrator
from costlens.money import parse_usd_to_nanos

REF = date(2026, 11, 3)


def parse(conn, text):
    return budgets.parse_budget(text, Orchestrator(conn).vocabulary(), REF)


def make(conn, text):
    r = parse(conn, text)
    assert r["status"] == "ok", r
    return budgets.create(conn, budgets.BudgetSpec.from_dict(r["spec"]), text)


# ---------------------------------------------------------------- parsing

def test_parse_full_sentence(demo_conn):
    r = parse(demo_conn, "Set up a monthly budget of $5,000 for prod-data and alert ravi@example.com at 80% and 100%")
    s = r["spec"]
    assert s["amount"] == "5000.00" and s["period"] == "monthly" and s["filters"] == {"account": ["310987654321"]}
    assert s["thresholds"] == [{"pct": 80, "type": "actual"}, {"pct": 100, "type": "actual"}]
    assert s["recipients"] == ["ravi@example.com"]
    assert r["scope"][0]["values"] == ["prod-data (310987654321)"]


@pytest.mark.parametrize("text,amount,period", [
    ("create a budget of 2k usd for EC2 in staging", "2000.00", "monthly"),
    ("budget 1.5m dollars yearly", "1500000.00", "yearly"),
    ("new quarterly budget of US$ 12,500.50 for team checkout", "12500.50", "quarterly"),
])
def test_parse_amounts_and_periods(demo_conn, text, amount, period):
    s = parse(demo_conn, text)["spec"]
    assert (s["amount"], s["period"]) == (amount, period)


def test_forecast_threshold_detection(demo_conn):
    s = parse(demo_conn, "set up a budget of $3000 for sandbox, email a@b.co at 90% and when the forecast crosses 110%")["spec"]
    assert {"pct": 90, "type": "actual"} in s["thresholds"]
    assert {"pct": 110, "type": "forecasted"} in s["thresholds"]


def test_defaults_and_assumptions(demo_conn):
    r = parse(demo_conn, "new budget $300 for untagged spend")
    assert [t["pct"] for t in r["spec"]["thresholds"]] == [80, 100, 100]
    assert r["spec"]["filters"] == {"tag:user:team": ["(untagged)"]}
    assert any("No email" in a for a in r["assumptions"])
    assert any("monthly" in a for a in r["assumptions"])


@pytest.mark.parametrize("text,msg", [
    ("set up budget of 5 lakh for sandbox", "US dollars"),
    ("set up a budget of ₹40000 for prod", "US dollars"),
    ("set up a budget for prod", "amount"),
    ("", "Describe"),
])
def test_parse_clarifies(demo_conn, text, msg):
    r = parse(demo_conn, text)
    assert r["status"] == "clarify" and msg in r["message"]


def test_spec_validation():
    good = {"name": "x", "amount": "10", "period": "monthly", "thresholds": [{"pct": 80, "type": "actual"}]}
    assert budgets.BudgetSpec.from_dict(good).amount == "10.00"
    for bad in [dict(good, amount="0"), dict(good, amount="-5"), dict(good, amount="abc"),
                dict(good, period="weekly"), dict(good, thresholds=[]),
                dict(good, thresholds=[{"pct": 0, "type": "actual"}]),
                dict(good, thresholds=[{"pct": 80, "type": "actual"}, {"pct": 80, "type": "actual"}]),
                dict(good, recipients=["not-an-email"]), dict(good, filters={"bad": ["x"]}),
                dict(good, name=""), dict(good, extra=1), dict(good, recipients=[f"u{i}@x.co" for i in range(11)])]:
        with pytest.raises(budgets.BudgetError):
            budgets.BudgetSpec.from_dict(bad)


# ---------------------------------------------------------------- forecast

def test_forecast_math_is_reproducible(demo_conn):
    start, end = budgets.period_window("monthly", REF)
    assert (start, end) == (date(2026, 11, 1), date(2026, 12, 1))
    f = budgets.forecast(demo_conn, {}, "unblended", start, end)
    actual = demo_conn.execute("SELECT SUM(unblended_nanos) FROM line_items WHERE usage_date>='2026-11-01'").fetchone()[0]
    last7 = [r[0] for r in demo_conn.execute(
        "SELECT SUM(unblended_nanos) FROM line_items WHERE usage_date BETWEEN '2026-10-27' AND '2026-11-02' GROUP BY usage_date")]
    rate = sum(last7) // 7
    assert f["actual_nanos"] == actual and f["days_left"] == 28
    assert f["forecast_nanos"] == actual + rate * 28
    assert f["confidence"] == "low"
    assert f["actual_nanos"] <= f["forecast_nanos"]


def test_forecast_quarter_and_year_windows():
    assert budgets.period_window("quarterly", REF) == (date(2026, 10, 1), date(2027, 1, 1))
    assert budgets.period_window("yearly", REF) == (date(2026, 1, 1), date(2027, 1, 1))
    # reference date is "the day after data": 1 Jan means the period is still December
    assert budgets.period_window("monthly", date(2027, 1, 1)) == (date(2026, 12, 1), date(2027, 1, 1))


def test_forecast_reports_missing_months(demo_conn):
    f = budgets.forecast(demo_conn, {}, "unblended", date(2026, 7, 1), date(2026, 12, 1))
    assert any("Missing billing data for 2026-07" in n for n in f["notes"])


def test_forecast_needs_data(fresh_conn):
    assert budgets.forecast(fresh_conn, {}, "unblended", date(2026, 11, 1), date(2026, 12, 1))["status"] == "insufficient_data"


# ---------------------------------------------------------------- evaluate + notify

class FakeNotifier:
    name = "fake"

    def __init__(self, result=("sent", "ok")):
        self.sent = []
        self.result = result

    def send(self, recipients, subject, body):
        self.sent.append((recipients, subject, body))
        return self.result


def test_alert_fires_once_per_period(demo_conn):
    bid = make(demo_conn, "budget of $5000 for prod-data, email ops@x.co at 10% and when forecast exceeds 100%")
    n = FakeNotifier()
    first = budgets.evaluate(demo_conn, n)
    assert {h["threshold"] for h in first} == {"actual-10", "forecasted-100"}
    assert budgets.evaluate(demo_conn, n) == []
    assert len(n.sent) == 2
    recips, subject, body = n.sent[0]
    assert recips == ["ops@x.co"] and "[SYNTHETIC DATA]" in subject and "SYNTHETIC" in body
    st = next(b for b in budgets.list_status(demo_conn) if b["id"] == bid)
    assert st["state"] == "forecast_exceeded"
    assert all(a["notification"]["delivery_status"] == "sent" for a in st["alerts"] if a["triggered"])


def test_failed_or_unsent_alerts_are_retried(demo_conn):
    make(demo_conn, "budget of $100 for sandbox, email ops@x.co at 50%")
    assert budgets.evaluate(demo_conn, notify.OutboxNotifier())[0]["status"] == "not_sent_no_provider"
    ok = FakeNotifier()
    assert budgets.evaluate(demo_conn, ok)[0]["status"] == "sent"
    assert budgets.evaluate(demo_conn, ok) == []
    assert demo_conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_no_recipients_is_recorded_not_sent(demo_conn):
    make(demo_conn, "budget of $100 for sandbox at 50%")
    n = FakeNotifier()
    h = budgets.evaluate(demo_conn, n)
    assert h[0]["status"] == "no_recipients" and n.sent == []


def test_untriggered_budget_sends_nothing(demo_conn):
    make(demo_conn, "budget of $1,000,000 monthly, email a@b.co at 80%")
    assert budgets.evaluate(demo_conn, FakeNotifier()) == []


def test_delete_budget(demo_conn):
    bid = make(demo_conn, "budget of $100 for sandbox")
    assert budgets.delete(demo_conn, bid) and not budgets.delete(demo_conn, bid)


def test_smtp_notifier_with_fake_server(monkeypatch):
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls["conn"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            calls["tls"] = True

        def login(self, u, p):
            calls["login"] = u

        def send_message(self, msg):
            calls["msg"] = msg

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("COSTLENS_SMTP_HOST", "email-smtp.ap-south-1.amazonaws.com")
    monkeypatch.setenv("COSTLENS_SMTP_FROM", "alerts@company.com")
    monkeypatch.setenv("COSTLENS_SMTP_USERNAME", "user")
    monkeypatch.setenv("COSTLENS_SMTP_PASSWORD", "secret-value")
    n = notify.get_notifier()
    assert isinstance(n, notify.SmtpNotifier)
    status, detail = n.send(["a@b.co"], "Budget alert\r\nBcc: evil@x.co", "body")
    assert status == "sent" and "secret" not in detail
    assert calls["tls"] and calls["login"] == "user" and calls["conn"][1] == 587
    assert "\n" not in calls["msg"]["Subject"] and calls["msg"]["Bcc"] is None
    assert "secret" not in str(notify.describe())


def test_smtp_failure_is_reported(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(notify.smtplib, "SMTP", Boom)
    n = notify.SmtpNotifier("smtp.example.com", "alerts@company.com")
    assert n.send(["a@b.co"], "s", "b")[0] == "failed"


def test_outbox_is_default(monkeypatch):
    monkeypatch.delenv("COSTLENS_SMTP_HOST", raising=False)
    assert isinstance(notify.get_notifier(), notify.OutboxNotifier)
    assert notify.describe()["configured"] is False


def test_budget_amount_matches_threshold_level(demo_conn):
    bid = make(demo_conn, "budget of $1234.56 for sandbox at 50%")
    st = next(b for b in budgets.list_status(demo_conn) if b["id"] == bid)
    assert st["alerts"][0]["level_display"] == "$617.28"
    assert parse_usd_to_nanos(st["spec"]["amount"]) == 1_234_560_000_000
