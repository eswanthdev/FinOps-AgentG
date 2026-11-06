"""Browser checks against a fake cloud transport; no paid AWS/model calls."""
import os
import threading

import pytest

from costlens.server import serve
from tests.test_cloud_app import cloud_client

pw=pytest.importorskip("playwright.sync_api")
CHROME=os.environ.get("COSTLENS_CHROME")
pytestmark=pytest.mark.skipif(not CHROME,reason="set COSTLENS_CHROME")


@pytest.fixture
def cloud_page(tmp_path,cloud_client):
    httpd=serve(str(tmp_path/"ui-state.sqlite"),"127.0.0.1",0)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    with pw.sync_playwright() as runtime:
        browser=runtime.chromium.launch(executable_path=CHROME,args=["--no-sandbox"])
        page=browser.new_page(viewport={"width":1400,"height":950})
        errors=[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{httpd.server_address[1]}")
        page.wait_for_selector(".data-card .dc-head")
        yield page,cloud_client,errors
        browser.close()
    httpd.shutdown();httpd.server_close()


def send(page):
    page.fill("#question","Show October cost")
    page.press("#question","Enter")
    page.get_by_text("Synthetic cost is $3.00.",exact=True).wait_for()
    page.locator(".card-title").filter(has_text="Unblended cost").wait_for()


def test_cloud_chat_renders_chart_and_pinned_dashboard(cloud_page,tmp_path):
    page,requests,errors=cloud_page
    assert page.locator("#modePill").inner_text()=="AI-assisted · Cloud"
    assert page.locator("#dataPill").is_visible()
    send(page)
    assert page.locator(".kpi-value").filter(has_text="$3.00").is_visible()
    page.get_by_role("button",name="Pin",exact=True).click()
    page.locator('[data-view="dashboards"]').click()
    page.locator("#pinGrid .card-title").filter(has_text="Unblended cost").wait_for()
    page.locator("#pinGrid .kpi-value").filter(has_text="$3.00").wait_for()
    assert len([operation for operation,_ in requests if operation=="query"])>=2
    page.screenshot(path=str(tmp_path/"cloud-dashboard.png"),full_page=True)
    assert not errors


def test_cloud_report_refreshes_against_cloud_data(cloud_page):
    page,requests,errors=cloud_page
    send(page)
    page.get_by_role("button",name="Save as report",exact=True).click()
    page.locator('[data-view="reports"]').click()
    page.locator('#reportsPage button').filter(has_text="Open").click()
    page.get_by_text("Saved answer, with current figures recalculated from CUR.",exact=True).wait_for()
    assert any(operation=="query" for operation,_ in requests)
    assert not errors


def test_cloud_sources_and_unsupported_modules_are_clear(cloud_page,tmp_path):
    page,requests,errors=cloud_page
    assert page.locator('[data-view="govern"]').is_disabled()
    assert page.locator('[data-view="budgets"]').is_disabled()
    page.locator('[data-view="optimize"]').click()
    page.get_by_role("heading",name="Optimize",exact=True).wait_for()
    page.get_by_text("Monthly savings estimate",exact=True).wait_for()
    assert page.locator(".opt-totals").get_by_text("$3,272.06",exact=True).is_visible()
    page.get_by_role("tab",name="Plan ready").click()
    page.locator("tr[data-opportunity]").first.click()
    assert page.locator(".opt-detail:visible").get_by_role("heading",name="Evidence",exact=True).is_visible()
    assert page.locator(".opt-detail:visible").get_by_role("heading",name="Calculation",exact=True).is_visible()
    assert page.get_by_role("button",name="Accept for planning",exact=True).count()==0
    page.screenshot(path=str(tmp_path/"optimize-restored.png"),full_page=True)
    page.locator('[data-view="data"]').click()
    page.get_by_text("The connection is live. Every account, resource and amount in the S3 datasets is synthetic.",exact=True).wait_for()
    assert "scope" in page.locator("#dataPage").inner_text().lower()
    assert not errors


def test_optimize_service_tabs_filter_recommendations_and_phases(cloud_page,tmp_path):
    page,requests,errors=cloud_page
    page.locator('[data-view="optimize"]').click()
    services=page.get_by_role("tablist",name="Services",exact=True)
    services.get_by_role("tab",name="RDS").click()
    page.get_by_text("Monthly savings estimate · RDS",exact=True).wait_for()
    assert page.locator("tr[data-opportunity]").count()==1
    assert "RDS" in page.locator("tr[data-opportunity]").inner_text() or "db-checkout-prod" in page.locator("tr[data-opportunity]").inner_text()
    services.get_by_role("tab",name="S3",exact=False).click()
    page.get_by_text("No S3 recommendations in this snapshot.",exact=True).wait_for()
    assert page.locator("tr[data-opportunity]").count()==0
    services.get_by_role("tab",name="EC2").click()
    page.get_by_text("Monthly savings estimate · EC2",exact=True).wait_for()
    page.get_by_role("tablist",name="Recommendation phases").get_by_role("tab",name="Plan ready").click()
    page.get_by_text("Monthly savings estimate · EC2",exact=True).wait_for()
    assert page.locator("tr[data-opportunity]").count()>0
    services.get_by_role("tab",name="All services").click()
    page.get_by_text("Monthly savings estimate",exact=True).wait_for()
    assert page.locator("tr[data-opportunity]").count()==26
    page.screenshot(path=str(tmp_path/"service-tabs.png"),full_page=True)
    assert not errors
