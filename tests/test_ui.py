"""Browser tests. Need Playwright and a Chrome/Chromium binary; skipped otherwise.

Set COSTLENS_CHROME to a browser path if it is not at /usr/bin/google-chrome.
"""
import os
import shutil
import threading

import pytest

from costlens.server import serve

pw = pytest.importorskip("playwright.sync_api")
expect = pw.expect
CHROME = os.environ.get("COSTLENS_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium binary found")


@pytest.fixture()
def app_url(demo_db_path):
    httpd = serve(str(demo_db_path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture()
def page(browser, app_url):
    pg = browser.new_page(viewport={"width": 1300, "height": 900})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.goto(app_url)
    pg.wait_for_selector(".data-card .dc-head")
    return pg


def send(pg, text):
    pg.fill("#question", text)
    pg.keyboard.press("Enter")


def test_empty_state_and_synthetic_label(page):
    assert page.locator(".greeting").is_visible()
    assert page.locator("#dataPill").is_visible()
    assert "SYNTHETIC" in page.locator(".data-card").inner_text()
    assert page.locator("#sendBtn").is_disabled()
    assert page.locator(".nav-item.planned").count() == 0  # every module is live
    assert not page.errors


def test_question_to_dashboard_and_controls(page):
    send(page, "Show cost by service for last month")
    page.wait_for_selector(".card .chart-wrap svg rect")
    card = page.locator(".card").last
    assert "$18,561.93" in card.locator(".kpis").inner_text()
    # Change group-by through the explorer control: re-runs the real query.
    card.locator(".ctl", has_text="Group by").locator("select").select_option("account")
    expect(page.locator(".card .card-title").last).to_contain_text("by account")
    assert "prod-data" in page.locator(".card").last.inner_text()
    # Table view and calculation evidence.
    page.locator(".card").last.get_by_role("button", name="Table").click()
    assert page.locator(".card table.tbl tfoot").first.inner_text().startswith("Total")
    page.locator("details.calc summary").first.click()
    assert "exec-2026-10-a" in page.locator("details.calc").first.inner_text()
    assert not page.errors


def test_variance_shows_drivers(page):
    send(page, "Why did costs increase in October compared to September?")
    page.wait_for_selector(".driver")
    text = page.locator(".card").last.inner_text()
    assert "BoxUsage:c6i.4xlarge" in text and "8 new" in text


def test_follow_up_chip(page):
    send(page, "Show cost by service for last month")
    page.wait_for_selector(".followup")
    page.locator(".followup", has_text="Now by account").click()
    expect(page.locator(".card")).to_have_count(2)
    assert "by account" in page.locator(".card .card-title").last.inner_text()


def test_insufficient_and_unsupported_states(page):
    send(page, "cost by service in july")
    page.wait_for_selector("text=Not enough data to answer")
    assert "missing" in page.locator(".card").last.inner_text()
    send(page, "how can I optimize EC2")
    page.wait_for_selector(".module-note")
    assert "Optimize" in page.locator(".module-note").last.inner_text()


def test_error_state_with_retry(page):
    page.route("**/api/ask", lambda route: route.fulfill(status=500, content_type="application/json",
                                                          body='{"error": "Internal error. Check the server log."}'))
    send(page, "Show cost by service for last month")
    page.wait_for_selector(".notice.error")
    assert page.get_by_role("button", name="Retry").is_visible()
    page.unroute("**/api/ask")
    page.get_by_role("button", name="Retry").click()
    page.wait_for_selector(".card .chart-wrap svg")


def test_pin_and_dashboard(page):
    send(page, "Show cost by service for last month")
    page.wait_for_selector(".card .chart-wrap svg")
    page.locator(".card").last.get_by_role("button", name="Pin").click()
    expect(page.locator("#pinCount")).to_have_text("1")
    page.click(".nav-item[data-view=dashboards]")
    page.wait_for_selector("#pinGrid .card .kpis")
    page.locator("#pinGrid").get_by_role("button", name="Remove").click()
    page.wait_for_selector("text=No saved dashboards yet.")


def test_data_page(page):
    page.click(".nav-item[data-view=data]")
    page.wait_for_selector(".panel")
    t = page.locator("#dataPage").inner_text()
    for s in ("rejected incomplete", "duplicate", "superseded", "missing", "Cost Optimization Hub"):
        assert s in t


def test_mobile_layout_has_no_horizontal_scroll(browser, app_url):
    pg = browser.new_page(viewport={"width": 375, "height": 800})
    pg.goto(app_url)
    pg.wait_for_selector(".suggestion")
    pg.locator(".suggestion").nth(1).click()
    pg.wait_for_selector(".card .chart-wrap svg")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 375
    pg.click("#menuBtn")
    assert pg.locator(".app.nav-open").count() == 1


def test_untrusted_text_is_not_rendered_as_html(page):
    send(page, '<img src=x onerror="window.__xss=1"> cost by service last month')
    page.wait_for_selector(".card")
    assert page.evaluate("window.__xss") is None
    assert page.locator(".msg-user .bubble img").count() == 0


def test_sidebar_order(page):
    labels = [t.strip() for t in page.locator(".nav > .nav-item").all_inner_texts()]
    order = [next(i for i, l in enumerate(labels) if l.startswith(x)) for x in ("Visualize", "Optimize", "Govern", "Anomalies", "Budgets")]
    assert order == sorted(order)


def test_anomalies_page_and_status(page):
    page.click(".nav-item[data-view=anomalies]")
    page.wait_for_selector("[data-anomaly] .chart-wrap svg")
    expect(page.locator("[data-anomaly]")).to_have_count(2)
    assert "8 new resource(s)" in page.locator("[data-anomaly]").first.inner_text()
    page.locator("[data-anomaly]").first.get_by_role("button", name="Dismiss").click()
    expect(page.locator("[data-anomaly]")).to_have_count(1)
    expect(page.locator("#anomalyCount")).to_have_text("1")
    page.locator("[data-anomaly]").first.get_by_role("button", name="Investigate in Visualize").click()
    page.wait_for_selector("#thread .card .chart-wrap svg")
    assert "by usage type" in page.locator("#thread .card .card-title").last.inner_text()


def test_budget_plain_english_flow(page):
    page.click(".nav-item[data-view=budgets]")
    page.wait_for_selector(".create-box textarea")
    page.fill(".create-box textarea", "Set up a monthly budget of $5,000 for prod-data and email ravi@example.com at 80% and 100%, and when the forecast crosses 100%")
    page.get_by_role("button", name="Preview budget").click()
    page.wait_for_selector("text=Review budget before creating")
    assert "prod-data (310987654321)" in page.locator(".create-box").inner_text()
    assert page.locator("[data-budget]").count() == 0  # nothing saved before confirmation
    page.get_by_role("button", name="Create budget").click()
    page.wait_for_selector("[data-budget]")
    card = page.locator("[data-budget]").first
    assert "Forecast over budget" in card.inner_text() and "ravi@example.com" in card.inner_text()
    assert "Saved to outbox" in page.locator(".panel", has_text="Alert outbox").inner_text()
    expect(page.locator("#budgetCount")).to_have_text("1")
    card.get_by_role("button", name="Delete").click()
    card.get_by_role("button", name="Confirm delete").click()
    page.wait_for_selector("text=No budgets yet.")


def test_budget_preview_validation_error(page):
    page.click(".nav-item[data-view=budgets]")
    page.fill(".create-box textarea", "budget of $500 for sandbox, email a@b.co at 50%")
    page.get_by_role("button", name="Preview budget").click()
    page.wait_for_selector("text=Review budget before creating")
    page.get_by_label("Email alerts to").fill("not-an-email")
    page.get_by_role("button", name="Create budget").click()
    page.wait_for_selector(".create-box .notice.error:not([hidden])")
    assert "Not a valid email" in page.locator(".create-box .notice.error").inner_text()


def test_chat_budget_and_forecast(page):
    send(page, "Set up a monthly budget of $5,000 for prod-data and alert ops@example.com at 80% and 100%")
    page.wait_for_selector("text=Review budget before creating")
    page.get_by_role("button", name="Create budget").click()
    page.wait_for_selector("text=View in Budgets & forecasting")
    send(page, "Forecast prod-data cost for this month")
    page.wait_for_selector("text=Forecast · November 2026")
    assert not page.errors


def open_optimize(page):
    page.click(".nav-item[data-view=optimize]")
    page.wait_for_selector("[data-opportunity]")


def test_optimize_page_list_and_totals(page):
    expect(page.locator("#optimizeCount")).to_have_text("14")
    open_optimize(page)
    assert page.locator("#topbarTitle").inner_text() == "Optimize"
    totals = page.locator(".opt-totals").inner_text()
    assert "$3,272.06" in totals and "Not measured" in totals and "not a savings number" in totals
    heads = page.locator(".opt-table thead").inner_text()
    for h in ("Owner", "Phase", "Status", "Monthly estimate", "Confidence"):
        assert h.lower() in heads.lower()
    assert "Plan ready (14)" in page.locator(".opt-tabs").inner_text()
    assert not page.errors


def test_optimize_finding_detail_shows_evidence_and_plan(page):
    open_optimize(page)
    page.locator("[data-opportunity]").first.click()
    d = page.locator(".opt-detail").first
    expect(d).to_be_visible()
    t = d.inner_text()
    for s in ("Evidence", "Time range", "Calculation", "Risks", "Limitations", "Action plan", "Prerequisites",
              "Validation", "Rollback", "Not measured"):
        assert s in t, s


def test_optimize_needs_evidence_tab_has_no_number(page):
    open_optimize(page)
    page.locator(".opt-tabs button", has_text="Needs evidence").click()
    expect(page.locator("[data-opportunity]")).to_have_count(5)
    assert "Not estimated" in page.locator(".opt-table").inner_text()
    page.locator("[data-opportunity]").first.click()
    assert "Next step" in page.locator(".opt-detail").first.inner_text()


def test_optimize_dismiss_changes_total(page):
    open_optimize(page)
    page.locator("[data-opportunity]").first.click()
    page.locator(".opt-detail").first.get_by_role("button", name="Dismiss").click()
    expect(page.locator(".opt-totals")).to_contain_text("$2,537.68")
    page.locator(".opt-tabs button", has_text="Dismissed").click()
    expect(page.locator("[data-opportunity]")).to_have_count(1)


def test_optimize_chat_on_page(page):
    open_optimize(page)
    page.fill(".opt-chat textarea", "How can I reduce EC2 cost?")
    page.keyboard.press("Enter")
    page.wait_for_selector(".opt-answer .card")
    assert "estimate" in page.locator(".opt-answer").inner_text().lower()
    page.fill(".opt-chat textarea", "How can I reduce S3 cost?")
    page.get_by_role("button", name="Ask", exact=True).click()
    expect(page.locator(".opt-answer")).to_contain_text("no recommendations")


def test_optimize_from_main_chat_and_evidence_link(page):
    send(page, "How can I reduce EC2 cost?")
    page.wait_for_selector(".msg-body .module-note")
    page.get_by_role("button", name="in Optimize").click()
    page.wait_for_selector("[data-opportunity]")
    page.locator("[data-opportunity]").first.click()
    page.locator(".opt-detail").first.get_by_role("button", name="Open evidence in Visualize").click()
    page.wait_for_selector(".card .chart-wrap svg")
    assert page.locator("#topbarTitle").inner_text() == "Visualize"
    assert not page.errors


def test_optimize_mobile_has_no_horizontal_scroll(browser, app_url):
    pg = browser.new_page(viewport={"width": 375, "height": 800})
    pg.goto(app_url)
    pg.wait_for_selector(".suggestion")
    pg.click("#menuBtn")
    pg.click(".nav-item[data-view=optimize]")
    pg.wait_for_selector("[data-opportunity]")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 375
    # The key columns must be readable without swiping sideways.
    assert pg.evaluate("(() => { const w = document.querySelector('.opt-table').closest('.tbl-wrap'); return w.scrollWidth <= w.clientWidth; })()")
    row = pg.locator("[data-opportunity]").first
    for label in ("Monthly estimate", "Confidence", "Phase", "Status"):
        cell = row.locator(f"td[data-label='{label}']")
        assert cell.is_visible()
        assert cell.bounding_box()["x"] + cell.bounding_box()["width"] <= 375
    assert pg.locator(".opt-detail:visible").count() == 0  # detail rows stay closed until tapped
    row.click()
    d = pg.locator(".opt-detail").first
    expect(d).to_be_visible()
    assert "effort: very low" in d.inner_text()
    assert pg.evaluate("document.documentElement.scrollWidth") <= 375


def open_govern(page):
    page.click(".nav-item[data-view=govern]")
    page.wait_for_selector("[data-violation]")


def test_govern_page_totals_and_violations(page):
    expect(page.locator("#governCount")).to_have_text("6")
    open_govern(page)
    assert page.locator("#topbarTitle").inner_text() == "Govern"
    assert "$808.07" in page.locator(".gov-totals").inner_text()
    assert "recorded, not verified" in page.locator(".gov-totals").inner_text()
    expect(page.locator("[data-violation]")).to_have_count(6)
    first = page.locator("[data-violation]").first.inner_text()
    assert "i-0f0a0001g5" in first and "Limitations" in first
    assert not page.errors


def test_govern_guardrail_workflow_records_but_applies_nothing(page):
    open_govern(page)
    page.locator(".opt-tabs button", has_text="Guardrail drafts").click()
    card = page.locator("[data-guardrail]", has_text="No GPU instances")
    expect(card).to_contain_text("ec2:RunInstances")
    for s in ("does not apply this", "Checks", "Rollout", "Rollback", "Management account"):
        assert s in card.inner_text(), s
    card.get_by_role("button", name="Send for review").click()
    card = page.locator("[data-guardrail]", has_text="No GPU instances")
    expect(card).to_contain_text("In review")
    card.get_by_role("button", name="Record approval").click()
    card = page.locator("[data-guardrail]", has_text="No GPU instances")
    expect(card).to_contain_text("Approved (recorded, not verified)")


def test_govern_exception_moves_violation_out_of_open(page):
    open_govern(page)
    card = page.locator("[data-violation]").first
    card.locator("summary", has_text="Add an exception").click()
    card.get_by_label("Exception owner").fill("data-platform")
    card.get_by_label("Exception reason").fill("Research project")
    card.get_by_label("Exception expiry").fill("2026-12-31")
    card.get_by_role("button", name="Add exception").click()
    expect(page.locator(".gov-totals")).to_contain_text("1 excepted")
    expect(page.locator("[data-violation]", has_text="Research project")).to_contain_text("excepted")


def test_govern_policy_toggle(page):
    open_govern(page)
    page.locator(".opt-tabs button", has_text="Policies").click()
    card = page.locator("[data-policy=approved-regions]")
    expect(card).to_contain_text("Compliant")
    card.get_by_role("button", name="Turn off").click()
    expect(page.locator("[data-policy=approved-regions]")).to_contain_text("Off")


def test_govern_chat_on_page_and_main_chat(page):
    open_govern(page)
    page.fill(".gov-chat textarea", "Which spend has no owner?")
    page.keyboard.press("Enter")
    expect(page.locator(".opt-answer")).to_contain_text("$808.07")
    page.click(".nav-item[data-view=chat]")
    send(page, "Draft a guardrail for GPUs in sandbox")
    page.wait_for_selector(".msg-body [data-guardrail]")
    assert "Govern" in page.locator(".module-note").last.inner_text()
    assert not page.errors


def test_govern_mobile_has_no_horizontal_scroll(browser, app_url):
    pg = browser.new_page(viewport={"width": 375, "height": 800})
    pg.goto(app_url)
    pg.wait_for_selector(".suggestion")
    pg.click("#menuBtn")
    pg.click(".nav-item[data-view=govern]")
    pg.wait_for_selector("[data-violation]")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 375
    pg.locator(".opt-tabs button", has_text="Guardrail drafts").click()
    pg.wait_for_selector("[data-guardrail]")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 375


@pytest.fixture()
def savings_page(browser, savings_db_path):
    httpd = serve(str(savings_db_path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    pg = browser.new_page(viewport={"width": 1300, "height": 900})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{httpd.server_address[1]}")
    pg.wait_for_selector(".data-card .dc-head")
    yield pg
    httpd.shutdown()
    httpd.server_close()


def open_realised(pg):
    pg.click(".nav-item[data-view=optimize]")
    pg.wait_for_selector("[data-opportunity]")
    pg.locator(".opt-tabs button", has_text="Realised").click()


def test_realised_tab_shows_states_and_stays_apart(savings_page):
    open_realised(savings_page)
    expect(savings_page.locator("[data-change]")).to_have_count(9)
    text = savings_page.locator("#optimizePage").inner_text()
    for s in ("Verified", "Partly realised", "Not realised", "Cannot verify", "Provisional", "Waiting", "never added to estimates"):
        assert s in text, s
    assert "$735.85" in savings_page.locator(".opt-totals").inner_text()
    assert not savings_page.errors


def test_realised_card_shows_split_and_evidence_link(savings_page):
    open_realised(savings_page)
    card = savings_page.locator("[data-change]", has_text="Rightsize: i-0c0a0000m6i")
    expect(card).to_contain_text("Commitment effect")
    expect(card).to_contain_text("-$33.87")
    card.get_by_role("button", name="After in Visualize").click()
    savings_page.wait_for_selector(".card .chart-wrap svg")
    assert savings_page.locator("#topbarTitle").inner_text() == "Visualize"


def test_record_and_undo_on_the_main_demo(page):
    open_optimize(page)
    expect(page.locator(".opt-totals")).to_contain_text("$3,272.06")
    page.locator("[data-opportunity]").first.click()
    d = page.locator(".opt-detail").first
    d.locator("summary", has_text="Record that this was done").click()
    d.get_by_label("Date implemented").fill("2026-10-20")
    d.get_by_role("button", name="Record as implemented").click()
    card = page.locator("[data-change]")
    expect(card).to_have_count(1)
    expect(page.locator(".opt-totals")).to_contain_text("$2,537.68")
    card.get_by_role("button", name="Undo record").click()
    expect(page.locator(".opt-totals")).to_contain_text("$3,272.06")


def test_realised_tab_on_mobile(browser, savings_db_path):
    httpd = serve(str(savings_db_path), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    pg = browser.new_page(viewport={"width": 375, "height": 800})
    pg.goto(f"http://127.0.0.1:{httpd.server_address[1]}")
    pg.wait_for_selector(".suggestion")
    pg.click("#menuBtn")
    open_realised(pg)
    pg.wait_for_selector("[data-change]")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 375
    httpd.shutdown()
    httpd.server_close()

def test_investigation_card_shows_answer_trace_and_evidence(page, monkeypatch):
    from costlens.agent import investigator
    from costlens.agent.investigator import ModelTurn
    query = {"kind": "explore", "start": "2026-10-01", "end": "2026-11-01", "metric": "unblended", "granularity": "total", "group_by": "service",
             "filters": [], "top_n": 5, "compare_start": "", "compare_end": ""}

    class Fake:
        turns = [ModelTurn("tool_use", [{"type": "tool_use", "id": "t1", "name": "query_costs", "input": query}], 1, 1),
                 ModelTurn("end_turn", [{"type": "text", "text": "<img src=x onerror=\"window.__xss=1\"> October cost $18,561.93."}], 1, 1)]

        def create(self, *_):
            return self.turns.pop(0)

    monkeypatch.setattr(investigator, "make_client", lambda: Fake())
    send(page, "What was the October bill?")
    page.wait_for_selector(".inv-answer")
    assert "Investigation" in page.locator(".module-note").last.inner_text()
    assert "$18,561.93" in page.locator(".inv-answer").inner_text()
    assert page.evaluate("window.__xss") is None and page.locator(".inv-answer img").count() == 0
    page.locator(".inv-trace summary").click()
    assert "query_costs" in page.locator(".inv-trace").inner_text()
    page.get_by_role("button", name="Open evidence").click()
    page.wait_for_selector(".card .chart-wrap svg")
    assert not page.errors


def test_failed_model_shows_a_notice_and_a_rules_answer(page, monkeypatch):
    from costlens.agent import investigator

    class Down:
        def create(self, *_):
            raise investigator.ModelUnavailable("APIConnectionError")

    monkeypatch.setattr(investigator, "make_client", lambda: Down())
    send(page, "Show cost by service for last month")
    page.wait_for_selector(".card .chart-wrap svg")
    assert "fixed rules" in page.locator(".msg-body .notice").first.inner_text()

class ScriptedModel:
    model = "fake-model"

    def __init__(self, *texts):
        self.texts = list(texts)

    def create(self, *_):
        from costlens.agent.investigator import ModelTurn
        return ModelTurn("end_turn", [{"type": "text", "text": self.texts.pop(0)}], 10, 5)


def test_memory_page_add_and_forget(page):
    page.click(".nav-item[data-view=memory]")
    page.get_by_label("New memory").fill("I care about prod-data first")
    page.get_by_role("button", name="Remember").click()
    expect(page.locator("[data-memory]")).to_have_count(1)
    page.get_by_label("New memory").fill("The bill is $5,000")
    page.get_by_role("button", name="Remember").click()
    expect(page.locator("#toast")).to_contain_text("amounts")
    page.locator("[data-memory]").get_by_role("button", name="Forget").click()
    expect(page.locator("#memoryPage")).to_contain_text("Nothing remembered yet")
    assert all("status of 400" in e for e in page.errors)  # the refused amount above, and nothing else


def test_remember_in_chat(page):
    send(page, "Remember that sandbox belongs to research")
    expect(page.locator(".msg-body").last).to_contain_text("Saved to your memory")
    page.click(".nav-item[data-view=memory]")
    expect(page.locator("[data-memory]")).to_contain_text("sandbox belongs to research")


def test_conversation_is_saved_and_can_continue(page):
    send(page, "Show cost by service for last month")
    page.wait_for_selector(".card .chart-wrap svg")
    page.click(".nav-item[data-view=conversations]")
    page.wait_for_selector("[data-conversation]")
    page.locator("[data-conversation]").get_by_role("button", name="Continue").click()
    expect(page.locator(".msg-user .bubble").first).to_contain_text("Show cost by service")
    send(page, "Now by account")
    expect(page.locator(".msg-user .bubble")).to_have_count(2)
    page.click(".nav-item[data-view=conversations]")
    expect(page.locator("[data-conversation]")).to_have_count(1)
    assert not page.errors


def test_save_report_feedback_and_agent_runs(page, monkeypatch):
    from costlens.agent import investigator
    fake = ScriptedModel("Nothing unusual to report.")
    monkeypatch.setattr(investigator, "make_client", lambda: fake)
    send(page, "Anything to report?")
    page.wait_for_selector(".inv-answer")
    page.get_by_role("button", name="Good answer").click()
    expect(page.locator("#toast")).to_contain_text("feedback")
    page.get_by_role("button", name="Save as report").click()
    expect(page.locator("#toast")).to_contain_text("Saved to Reports")
    page.click(".nav-item[data-view=reports]")
    page.wait_for_selector("[data-report]")
    page.locator("[data-report]").get_by_role("button", name="Open").click()
    expect(page.locator("[data-report-detail]")).to_contain_text("Nothing unusual to report.")
    page.click(".nav-item[data-view=runs]")
    page.wait_for_selector("[data-run]")
    text = page.locator("#runsPage").inner_text()
    assert "1 up / 0 down" in text and "no verified model price" in text
    assert not page.errors


def test_agent_pages_on_mobile(browser, app_url):
    pg = browser.new_page(viewport={"width": 375, "height": 800})
    pg.goto(app_url)
    pg.wait_for_selector(".suggestion")
    for view in ("conversations", "memory", "reports", "runs"):
        pg.click("#menuBtn")
        pg.click(f".nav-item[data-view={view}]")
        pg.wait_for_selector(f"#{view}Page .page-head")
        assert pg.evaluate("document.documentElement.scrollWidth") <= 375, view