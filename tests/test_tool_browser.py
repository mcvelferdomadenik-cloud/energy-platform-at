"""The tool in a real browser: sign in, narrow the customers, calculate, see a refusal.

Headless Chromium against the app on a loopback port, with the stand-ins of `test_tool.py`; no
warehouse, no pricing engine. Skipped where Chromium is not installed, as in CI.
"""

import socket
import threading
from pathlib import Path

import pytest
import uvicorn

from megavolt_tool import main, session
from megavolt_tool.pricing import Portfolio, RateLimit, engine
from test_tool import POINT, FakeEngine, FakeStore, FakeSupabase, warmed

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect
with sync_api.sync_playwright() as driver:
    if not Path(driver.chromium.executable_path).exists():
        pytest.skip(
            "Chromium is not installed (playwright install chromium)", allow_module_level=True
        )

OTHER = POINT[:-1] + "B"


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """The app with its stand-ins, served on a free loopback port for this module."""
    with pytest.MonkeyPatch.context() as patch:
        record = FakeSupabase()
        patch.setattr(main, "store", FakeStore)
        patch.setattr(main, "portfolio", Portfolio(cache_dir=tmp_path_factory.mktemp("portfolio")))
        patch.setattr(main, "rate_limit", RateLimit())
        patch.setattr(main, "sign_in_limit", RateLimit(per_hour=main.SIGN_INS_PER_HOUR))
        patch.setattr(session, "supabase", lambda: record)
        patch.setitem(main.app.dependency_overrides, engine, FakeEngine)
        main.portfolio.current(FakeStore(), FakeEngine())  # the day is warm, as after start-up
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # lifespan off: the start-up thread would warm the portfolio with the real engine.
        server = uvicorn.Server(
            uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            assert thread.is_alive(), "the server did not start"
            thread.join(0.05)
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True
        thread.join(10)
        warmed()


def sign_in(page, served, password="right"):
    """Fill in the sign-in form as a person would."""
    page.goto(f"{served}/signin")
    page.get_by_label("Email").fill("a@megavolt.test")
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def test_a_wrong_password_shows_the_form_again_with_one_line(page, served):
    sign_in(page, served, password="wrong")
    expect(page.get_by_role("heading", level=1)).to_have_text("Sign in")
    expect(page.locator(".error")).to_have_text("sign-in failed")


def test_search_and_a_segment_chip_narrow_the_table(page, served):
    sign_in(page, served)
    rows = page.locator("#customers tbody tr")
    expect(rows).to_have_count(2)
    page.locator("label.chip", has_text="business").click()
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text(OTHER)
    expect(page.locator("#count")).to_have_text("1 of 2 registered")
    page.get_by_label("Search by metering point").fill("zzz")
    expect(page.locator("#customers")).to_contain_text('No customer matches "ZZZ"')


def test_calculating_both_contracts_shows_both_offers_the_reasons_and_the_day(page, served):
    sign_in(page, served)
    page.get_by_role("link", name=POINT).click()
    expect(page.locator("#chart-day rect")).to_have_count(24)
    expect(page.locator("input[name=contract][value=both]")).to_be_checked()
    page.get_by_role("button", name="Calculate").click()
    expect(page.locator("#offer-fixed")).to_contain_text("12.35 c/kWh")
    expect(page.locator("#offer-dynamic")).to_contain_text("391")
    expect(page.locator("#offer-reasons li")).to_contain_text(["Premium for unpredictability"])
    expect(page.locator("#offer-reasons .pill")).to_have_text("usual")


def test_a_refused_input_is_one_line_in_the_error_card(page, served):
    sign_in(page, served)
    page.goto(f"{served}/customers/DROP-TABLE")
    expect(page.locator(".error-card")).to_have_text("Not a metering point identifier.")


def test_the_portfolio_page_shows_both_tables_and_the_chart(page, served):
    sign_in(page, served)
    page.get_by_role("link", name="Portfolio").click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Portfolio")
    expect(page.locator("#segments tbody tr")).to_have_count(2)
    expect(page.locator("#offers tbody tr")).to_contain_text(["12.35", "12.35"])
    expect(page.locator("#chart-fixed svg[role=img]")).to_be_visible()
    expect(page.locator("#portfolio[hx-trigger]")).to_have_count(0)
    page.set_viewport_size({"width": 360, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth") <= 360
