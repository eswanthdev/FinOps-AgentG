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
