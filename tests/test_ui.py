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
    assert page.locator(".nav-item.planned").count() == 2
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
