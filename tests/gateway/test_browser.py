"""Real-browser test of the portal through the gateway (headless, installed Edge/Chrome channel).

Needs ``pip install playwright`` (no browser download: it drives the installed Microsoft Edge or
Google Chrome) and the local stack (scripts/local_run.py up). Skipped otherwise.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.gateway.test_gateway import GW, ROOT, _up, local_run, passwords

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(not _up(), reason="local stack not running (scripts/local_run.py up)")


@pytest.fixture
def browser():
    p = playwright.sync_playwright().start()
    b = None
    for channel in ("msedge", "chrome"):
        try:
            b = p.chromium.launch(channel=channel, headless=True)
            break
        except Exception:  # noqa: BLE001
            continue
    if b is None:
        p.stop()
        pytest.skip("no installed Edge/Chrome channel available to Playwright")
    yield b
    # The installed browser may be newer than this Playwright driver; a driver error while
    # shutting the browser down is test-tool noise, not a product failure.
    for close in (b.close, p.stop):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def sign_in(page, username, group="customer"):
    page.goto(GW + "/")
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", passwords()[group])
    page.click("button[type=submit]")
    page.wait_for_selector("#logout")


def test_customer_login_statement_request_download_and_logout(browser):
    ctx = browser.new_context(accept_downloads=True)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text else None)
    sign_in(page, "customer001")
    cookies = {c["name"]: c for c in ctx.cookies()}
    assert cookies["nvb_session"]["httpOnly"] is True and cookies["nvb_session"]["sameSite"] == "Lax"
    assert page.evaluate("document.cookie") == ""                      # the session cookie is invisible to scripts
    nav = page.inner_text("#nav")
    assert "Statements" in nav and "Loans" in nav and "Batches" not in nav and "Loan review" not in nav
    page.wait_for_selector("select[name=account] option", state="attached")
    page.select_option("select[name=period]", "2026-04")
    page.click("text=Request statement")
    page.locator("#statement-list").get_by_text("2026-04").first.wait_for()
    out = local_run("worker", "statements")
    assert out.returncode == 0
    page.wait_for_selector("#statement-list a[download]", timeout=20000)   # the list polls and shows the link
    with page.expect_download() as dl:
        page.click("#statement-list a[download] >> nth=0")
    path = dl.value.path()
    assert open(path, "rb").read(5) == b"%PDF-"
    # Loans page: decimal text is converted to integer minor units in the browser.
    page.click("#nav >> text=Loans")
    page.fill("input[placeholder='e.g. 120000.00']", "120000.50")
    page.fill("input[placeholder='e.g. 15000.00']", "15000")
    page.click("text=Save draft")
    page.wait_for_selector("#loan-list table")
    assert "120,000.50" in page.inner_text("#loan-list") and "draft" in page.inner_text("#loan-list")
    page.click("#loan-list >> text=Submit")
    page.locator("#loan-list").get_by_text("submitted").first.wait_for()
    page.click("#logout")
    page.wait_for_selector("input[name=password]")
    assert "nvb_session" not in {c["name"] for c in ctx.cookies()}
    resp = page.request.get(GW + "/x/statements/v1/requests")
    assert resp.status == 401
    assert errors == [], errors
    ctx.close()


def test_staff_pages_follow_extension_roles_not_the_core_role(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    sign_in(page, "demoload01", "demo")
    page.wait_for_selector("#subject")
    subject = page.inner_text("#subject")
    assert subject.startswith("nvb-user:")
    assert page.inner_text("#nav").strip() == "My identity"            # a core staff/demo role sees no staff pages
    for service, role in (("statements", "batch_operator"), ("loans", "batch_operator"), ("statements", "extension_admin")):
        assert subprocess.run([sys.executable, str(ROOT / "scripts" / "local_run.py"), "grant", service, role, subject],
                              capture_output=True, cwd=str(ROOT)).returncode == 0
    page.reload()
    page.wait_for_selector("#nav >> text=Batches")
    assert "Roles" in page.inner_text("#nav")
    page.click("#nav >> text=Batches")
    page.fill("input[pattern]", "2026-03")
    page.click("text=Create statement batch")
    page.wait_for_selector("#statement-batches table")
    assert "2026-03" in page.inner_text("#statement-batches")
    assert local_run("worker", "statements").returncode == 0
    page.locator("#statement-batches").get_by_text("completed", exact=True).first.wait_for(timeout=30000)
    ctx.close()
