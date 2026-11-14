"""The tool's routes: nothing without a session, roles, input shape, CSRF, the rate limit.

No warehouse and no pricing engine: both are stood in for, so these run in CI. What the engine
computes is its own tests' business.
"""

import hashlib
import math
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from megavolt_tool import main, session
from megavolt_tool.auth import ROLES, SESSION_COOKIE, User, current_user
from megavolt_tool.pricing import Portfolio, RateLimit, ToolError, by_segment, engine, shown
from megavolt_tool.session import Session, SessionError
from megavolt_tool.store import Customer, Store, StoreError, valid_metering_point

POINT = "AT09999902340TESTTOOLAAAAAAAAAAAA"
HTMX = {"HX-Request": "true", "Origin": "http://testserver"}
# The vendored htmx 2.0.4; a changed file is a changed dependency and must be looked at.
HTMX_SHA256 = "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447"


class FakeStore:
    """Two customers and nothing behind them."""

    def customers(self):
        return [
            Customer(POINT, "household", "H0", 3500.0, supplied_kwh=2400.0),
            Customer(POINT[:-1] + "B", "business", "G0", 6000.0),
        ]

    def customer(self, metering_point):
        valid_metering_point(metering_point)
        return next((c for c in self.customers() if c.metering_point == metering_point), None)

    def settled(self, metering_point):
        start = datetime(2025, 1, 1, tzinfo=UTC)
        return [
            (start + timedelta(minutes=15 * i), 1.0, 0.1 + (i % 96) / 960) for i in range(96 * 3)
        ]

    def prices(self):
        return [(datetime(2025, 1, 1, tzinfo=UTC), 100.0)]

    def spread_eur_kwh(self):
        return 0.05


class FakeSupabase:
    """Signs in one person, verifies one token, and keeps every audit row it is given."""

    def __init__(self):
        self.rows = []
        self.signed_out = []
        self.verified = []

    def sign_in(self, email, password):
        if password != "right":
            raise SessionError("sign-in failed")
        return Session("token-of-" + email.split("@")[0], "refresh", 3600)

    def sign_out(self, access_token):
        self.signed_out.append(access_token)

    def verify(self, access_token):
        self.verified.append(access_token)
        if access_token.startswith("token-of-"):
            return SimpleNamespace(user_id="u", email="a@megavolt.test", role="sales")
        raise SessionError("session not recognised")

    def record(self, access_token, row):
        self.rows.append((access_token, row))


class FakeEngine:
    """Answers like the private engine would, without any of its arithmetic."""

    asked_kwh = None

    def price(self, prices, settled, annual_kwh, spread, contract, others):
        FakeEngine.asked_kwh = annual_kwh
        fixed = SimpleNamespace(
            eur_kwh=0.123456,
            expected_eur_kwh=0.1,
            market_premium_eur_kwh=0.01,
            volume_premium_eur_kwh=0.005,
            margin_eur_kwh=0.006,
        )
        dynamic = SimpleNamespace(expected_bill_eur=391.4, high_bill_eur=590.6, fee_eur_kwh=0.015)
        return SimpleNamespace(
            fixed=fixed if contract in ("fixed", "both") else None,
            dynamic=dynamic if contract in ("dynamic", "both") else None,
            reasons=("premium for unpredictability: usual; about as much as most",),
        )

    def portfolio(self, prices, customers):
        return SimpleNamespace(
            high_errors=(0.1, 0.2, 0.3), market_ranges=(0.0, 0.01, 0.02), skipped=1
        )


@pytest.fixture
def record():
    return FakeSupabase()


@pytest.fixture
def cold_client(monkeypatch, record, tmp_path):
    monkeypatch.setattr(main, "store", FakeStore)
    monkeypatch.setattr(main, "portfolio", Portfolio(cache_dir=tmp_path))  # never the repo's data/
    monkeypatch.setattr(main, "rate_limit", RateLimit())
    monkeypatch.setattr(main, "sign_in_limit", RateLimit(per_hour=main.SIGN_INS_PER_HOUR))
    monkeypatch.setattr(session, "supabase", lambda: record)
    main.portfolio.reset()
    main.app.dependency_overrides[engine] = FakeEngine
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()
    warmed()


@pytest.fixture
def client(cold_client):
    """The app on a day whose portfolio is already calculated, as after the start-up warm."""
    main.portfolio.current(FakeStore(), FakeEngine())
    return cold_client


def warmed():
    """Wait for a background calculation a page view started, so it ends inside its own test."""
    for thread in threading.enumerate():
        if thread.name == main.WARMING:
            thread.join(10)


def signed_in_as(role):
    main.app.dependency_overrides[current_user] = lambda: User(
        f"{role}@megavolt.test", role, f"token-of-{role}"
    )


def every_route(routes):
    """Every path operation, however deeply a router is nested inside the app."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from every_route(inner.routes)
        elif getattr(route, "methods", None):
            yield route


# --- the gate -----------------------------------------------------------------------------------


def test_every_route_but_sign_in_refuses_a_request_without_a_session(client):
    routes = list(every_route(main.app.routes))
    assert "/signin" in {r.path for r in routes} and len(routes) >= 5
    for route in routes:
        for method in route.methods:
            if route.path == "/signin" or method not in ("GET", "POST"):
                continue
            path = route.path.replace("{metering_point}", POINT)
            # No htmx headers and an empty body: the gate must answer before anything else does.
            # A browser asking for a page is sent to sign in; anything else is a plain 401.
            response = client.request(method, path, data={}, follow_redirects=False)
            if method == "GET":
                assert response.status_code == 303, (path, response.status_code)
                assert response.headers["location"] == "/signin"
            else:
                assert response.status_code == 401, (method, path, response.status_code)


def test_a_session_cookie_that_does_not_verify_is_refused(client, record):
    client.cookies.set(SESSION_COOKIE, "stale")
    answer = client.post("/signout", headers=HTMX)
    assert answer.status_code == 401 and answer.headers["HX-Redirect"] == "/signin"
    assert record.verified == ["stale"]


def test_a_request_for_another_host_is_refused_before_anything_else(client):
    assert client.get("/signin", headers={"Host": "attacker.example"}).status_code == 400


# --- signing in and out ---------------------------------------------------------------------------

FORM = {"Origin": "http://testserver"}


def test_signing_in_sets_a_hardened_cookie_and_goes_to_the_customers(client):
    answer = client.post(
        "/signin",
        data={"email": "a@megavolt.test", "password": "right"},
        headers=FORM,
        follow_redirects=False,
    )
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    cookie = answer.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=token-of-a;")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=3600" in cookie


def test_a_wrong_password_shows_the_form_again_with_one_line(client):
    answer = client.post(
        "/signin", data={"email": "a@megavolt.test", "password": "wrong"}, headers=FORM
    )
    assert answer.status_code == 401 and "sign-in failed" in answer.text
    assert "set-cookie" not in answer.headers


def test_signing_in_from_another_origin_or_from_nowhere_is_refused(client):
    for headers in ({"Origin": "http://evil.example"}, {}):
        answer = client.post(
            "/signin", data={"email": "a@megavolt.test", "password": "right"}, headers=headers
        )
        assert answer.status_code == 403, headers


def test_an_account_without_a_role_cannot_sign_in(client, record):
    def no_role(token):
        raise SessionError("this account has no role in the tool")

    record.verify = no_role
    answer = client.post(
        "/signin", data={"email": "a@megavolt.test", "password": "right"}, headers=FORM
    )
    assert answer.status_code == 401 and "no role" in answer.text
    assert "set-cookie" not in answer.headers
    # The Supabase session that sign-in opened is closed again, not left alive for an hour.
    assert record.signed_out == ["token-of-a"]


def test_the_eleventh_sign_in_attempt_in_an_hour_is_refused(client):
    for _ in range(10):
        client.post("/signin", data={"email": "x@megavolt.test", "password": "wrong"}, headers=FORM)
    answer = client.post(
        "/signin", data={"email": "X@megavolt.test ", "password": "right"}, headers=FORM
    )
    assert answer.status_code == 401 and "too many" in answer.text


def test_a_form_that_does_not_fit_is_not_echoed_back(client):
    answer = client.post("/signin", data={"email": "a@megavolt.test"}, headers=FORM)
    assert answer.status_code == 422 and "not filled in" in answer.text
    long = client.post(
        "/signin", data={"email": "a@megavolt.test", "password": "p" * 2000}, headers=FORM
    )
    assert long.status_code == 422 and "ppp" not in long.text


def test_signing_out_ends_the_session_on_both_sides(client, record):
    signed_in_as("sales")
    answer = client.post("/signout", headers=HTMX)
    assert answer.status_code == 200 and answer.headers["HX-Redirect"] == "/signin"
    cookie = answer.headers["set-cookie"]
    assert f'{SESSION_COOKIE}=""' in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert record.signed_out == ["token-of-sales"]


def test_the_docs_pages_do_not_exist_even_for_an_admin(client):
    signed_in_as("admin")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_the_vendored_htmx_is_the_file_that_was_reviewed():
    digest = hashlib.sha256((Path(main.HERE) / "static" / "htmx.min.js").read_bytes()).hexdigest()
    assert digest == HTMX_SHA256


# --- roles and shape ----------------------------------------------------------------------------


def test_sales_sees_the_customers_and_prices_one(client):
    signed_in_as("sales")
    assert client.get("/").status_code == 200
    page = client.get(f"/customers/{POINT}")
    assert page.status_code == 200 and POINT in page.text
    assert "We supply" in page.text and "2,400" in page.text and "31<span" in page.text
    result = client.post(f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX)
    assert result.status_code == 200
    # The offer is for what we supply after the community's share, not the whole consumption.
    assert FakeEngine.asked_kwh == 2400.0
    assert "12.35 c/kWh" in result.text and "296 <span" in result.text
    assert "391 <span" in result.text and "591 €" in result.text
    assert "Premium for unpredictability" in result.text and "usual" in result.text
    assert "base" not in result.text


def test_every_calculation_is_recorded_as_the_person_before_it_is_shown(client, record):
    signed_in_as("sales")
    client.post(f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX)
    assert len(record.rows) == 1
    token, row = record.rows[0]
    assert token == "token-of-sales"
    assert (row["email"], row["role"], row["event"]) == ("sales@megavolt.test", "sales", "priced")
    assert (row["customer"], row["contract"]) == (POINT, "both")
    assert row["detail"]["fixed"]["eur_kwh"] == 0.1235


def test_a_refusal_and_a_portfolio_view_are_recorded_too(client, record):
    signed_in_as("analyst")

    class Refusing(FakeEngine):
        def price(self, *args):
            raise ToolError("cannot price this customer: the consumption does not cover a year")

    main.app.dependency_overrides[engine] = Refusing
    client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
    client.get("/portfolio")
    events = [row["event"] for _, row in record.rows]
    assert events == ["refused", "portfolio"]
    assert record.rows[0][1]["detail"]["reason"].startswith("cannot price")


def test_when_the_record_cannot_be_written_there_is_no_offer(client, record):
    signed_in_as("sales")

    def refuse(access_token, row):
        raise SessionError("the record could not be written")

    record.record = refuse
    answer = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
    assert answer.status_code == 503 and "could not be written" in answer.text
    assert "c/kWh" not in answer.text


def test_the_analyst_also_sees_the_components_and_the_portfolio(client):
    signed_in_as("analyst")
    result = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
    assert "base 10.0" in result.text and "margin 0.6" in result.text
    assert client.get("/portfolio").status_code == 200


def test_every_role_may_open_the_portfolio_and_nobody_else(client):
    for role in ROLES:
        signed_in_as(role)
        assert client.get("/portfolio").status_code == 200
        assert '<a href="/portfolio"' in client.get("/").text
    signed_in_as("guest")
    assert client.get("/portfolio").status_code == 403
    assert client.get("/portfolio/summary", headers=HTMX).status_code == 403


def test_a_malformed_metering_point_never_reaches_a_query(client):
    signed_in_as("sales")
    assert client.get("/customers/DROP-TABLE").status_code == 422
    post = client.post("/customers/DROP-TABLE/offer", data={"contract": "fixed"}, headers=HTMX)
    assert post.status_code == 422
    with pytest.raises(ValueError):
        valid_metering_point("AT0999990234" + "A" * 21)
    with pytest.raises(ValueError):
        valid_metering_point(POINT + "\n")


def test_an_unknown_customer_and_an_unknown_contract_are_refused(client):
    signed_in_as("sales")
    unknown = f"{POINT[:-1]}Z"
    assert client.get(f"/customers/{unknown}").status_code == 404
    post = client.post(f"/customers/{unknown}/offer", data={"contract": "fixed"}, headers=HTMX)
    assert post.status_code == 404
    post = client.post(f"/customers/{POINT}/offer", data={"contract": "flexible"}, headers=HTMX)
    assert post.status_code == 422


# --- the annual volume --------------------------------------------------------------------------

OFFER = f"/customers/{POINT}/offer"


def test_the_customer_page_has_a_labelled_annual_volume_field(client):
    signed_in_as("sales")
    page = client.get(f"/customers/{POINT}").text
    assert '<label for="annual_kwh" class="muted">Annual volume kWh</label>' in page
    assert 'id="annual_kwh" name="annual_kwh"' in page and 'placeholder="2,400"' in page
    assert 'pattern="[1-9][0-9]{0,6}"' in page and "1 to 1,000,000" in page


def test_an_empty_volume_prices_what_we_supply(client, record):
    signed_in_as("sales")
    for empty in ("", "  "):
        FakeEngine.asked_kwh = None
        result = client.post(OFFER, data={"contract": "both", "annual_kwh": empty}, headers=HTMX)
        assert result.status_code == 200 and FakeEngine.asked_kwh == 2400.0
        assert result.text.count("assuming 2,400 kWh a year") == 2
        assert "what we supply after the community" in result.text
        detail = record.rows[-1][1]["detail"]
        assert (detail["annual_kwh"], detail["volume_override"]) == (2400, False)


def test_a_typed_volume_replaces_what_we_supply_on_the_cards_and_in_the_record(client, record):
    signed_in_as("sales")
    result = client.post(OFFER, data={"contract": "both", "annual_kwh": " 4000 "}, headers=HTMX)
    assert result.status_code == 200 and FakeEngine.asked_kwh == 4000.0
    assert result.text.count("assuming 4,000 kWh a year") == 2
    # 12.35 c/kWh over 4,000 kWh, and the chart no longer calls it what we supply.
    assert "494 <span" in result.text and "296 <span" not in result.text
    assert "Both for 4,000 kWh a year." in result.text
    ((_, row),) = record.rows
    assert row["event"] == "priced"
    assert (row["detail"]["annual_kwh"], row["detail"]["volume_override"]) == (4000, True)
    for contract in ("fixed", "dynamic"):
        one = client.post(OFFER, data={"contract": contract, "annual_kwh": "1000000"}, headers=HTMX)
        assert one.status_code == 200 and FakeEngine.asked_kwh == 1_000_000.0
        assert one.text.count("assuming 1,000,000 kWh a year") == 1


@pytest.mark.parametrize(
    "typed",
    ["abc", "0", "-5", "1000001", "nan", "inf", "1e9", "1e3", "2500.5", "+5", "1,000", "0x10"]
    + ["007", "１２３", "12 34", "<b>7</b>", "9" * 400],
)
def test_a_volume_that_is_not_whole_kwh_within_the_bound_is_refused(client, record, typed):
    signed_in_as("sales")
    FakeEngine.asked_kwh = None
    result = client.post(OFFER, data={"contract": "both", "annual_kwh": typed}, headers=HTMX)
    assert result.status_code == 422 and "error-card" in result.text
    assert "The annual volume must be a whole number of kWh between 1 and 1,000,000." in result.text
    assert "<b>" not in result.text and "999" not in result.text and "c/kWh" not in result.text
    assert FakeEngine.asked_kwh is None
    ((_, row),) = record.rows
    assert row["event"] == "refused" and "annual volume" in row["detail"]["reason"]
    assert row["detail"]["annual_kwh"] is None and row["detail"]["volume_override"] is True


def test_a_refusal_of_a_typed_volume_by_the_engine_is_recorded_with_that_volume(client, record):
    signed_in_as("sales")

    class Refusing(FakeEngine):
        def price(self, *args):
            raise ToolError("cannot price this customer: the volume does not match the history")

    main.app.dependency_overrides[engine] = Refusing
    result = client.post(OFFER, data={"contract": "fixed", "annual_kwh": "9000"}, headers=HTMX)
    assert result.status_code == 422 and "does not match" in result.text
    detail = record.rows[0][1]["detail"]
    assert (detail["annual_kwh"], detail["volume_override"]) == (9000, True)


# --- CSRF and the rate limit --------------------------------------------------------------------


def test_a_post_without_the_htmx_header_or_from_another_origin_is_refused(client):
    signed_in_as("sales")
    assert client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}).status_code == 403
    for origin in ("http://evil.example", "http://testserver.evil.example", "https://testserver"):
        headers = {"HX-Request": "true", "Origin": origin}
        post = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=headers)
        assert post.status_code == 403, origin
    tricked = {"HX-Request": "true", "Referer": "http://testserver@evil.example/x"}
    post = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=tricked)
    assert post.status_code == 403


def test_the_rate_limit_stops_the_sixty_first_calculation_in_an_hour(client):
    signed_in_as("sales")
    for _ in range(60):
        post = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
        assert post.status_code == 200
    refused = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
    assert refused.status_code == 422 and "Too many calculations" in refused.text


def test_the_rate_limit_is_per_person_and_forgets_after_an_hour():
    limit = RateLimit(per_hour=2)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    limit.check("a", start)
    limit.check("a", start)
    with pytest.raises(ToolError):
        limit.check("a", start)
    limit.check("b", start)
    limit.check("a", start.replace(hour=1, minute=1))


# --- what goes wrong is a line, never a trace ---------------------------------------------------


def test_an_engine_that_refuses_is_shown_as_a_plain_message(client):
    signed_in_as("sales")

    class Refusing(FakeEngine):
        def price(self, *args):
            raise ToolError("cannot price this customer: the consumption does not cover a year")

    main.app.dependency_overrides[engine] = Refusing
    result = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
    assert result.status_code == 422 and "cover a year" in result.text


def test_a_missing_engine_and_a_missing_warehouse_are_never_a_500(cold_client, monkeypatch):
    client = cold_client
    signed_in_as("analyst")
    main.app.dependency_overrides.pop(engine)  # the real wrapper, without the private package
    # Whether or not the private package happens to be installed here, the tool must behave as
    # if it were not: the public test suite never depends on it.
    monkeypatch.setitem(sys.modules, "megavolt_pricing", None)
    result = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
    assert result.status_code == 422 and "still being calculated" in result.text
    assert summary_saying(client, "not installed").status_code == 422
    warmed()
    result = client.post(f"/customers/{POINT}/offer", data={"contract": "fixed"}, headers=HTMX)
    assert result.status_code == 422 and "not installed" in result.text
    assert client.get("/portfolio").status_code == 422

    class Down(FakeStore):
        def customers(self):
            raise StoreError("the warehouse is not reachable")

    monkeypatch.setattr(main, "store", Down)
    listing = client.get("/")
    assert listing.status_code == 503 and "not reachable" in listing.text
    assert "Traceback" not in listing.text


def test_a_figure_that_is_not_a_number_is_refused_before_it_is_shown():
    odd = SimpleNamespace(
        fixed=SimpleNamespace(
            eur_kwh=math.nan,
            expected_eur_kwh=0.1,
            market_premium_eur_kwh=0.0,
            volume_premium_eur_kwh=0.0,
            margin_eur_kwh=0.0,
        ),
        dynamic=None,
        reasons=(),
    )
    with pytest.raises(ToolError, match="not a number"):
        shown(odd)


def test_the_audit_record_carries_only_rounded_figures():
    view = shown(FakeEngine().price([], [], 1.0, 0.0, "both", None))
    for value in (*view["fixed"].values(), view["dynamic"]["fee_eur_kwh"]):
        assert value == round(value, 4)
    assert view["dynamic"]["expected_bill_eur"] == 391


def test_the_store_refuses_a_connection_string_that_is_not_the_reader(monkeypatch):
    with pytest.raises(StoreError, match="megavolt_reader"):
        Store("postgresql://megavolt_writer:x@127.0.0.1:5433/megavolt")
    monkeypatch.setenv("TOOL_DSN", "postgresql://megavolt:x@127.0.0.1:5433/megavolt")
    with pytest.raises(StoreError, match="must log in as megavolt_reader"):
        Store()


def test_the_list_narrows_by_typed_search_and_by_segment(client):
    client.cookies.set(SESSION_COOKIE, signed_in_as("sales"))
    assert client.get("/").text.count("<tr>") == 3  # header + two customers
    narrowed = client.get("/", params={"q": POINT[:-1].lower() + "b"}).text
    assert POINT not in narrowed and POINT[:-1] + "B" in narrowed
    assert "2 of 2" not in narrowed and "1 of 2" in narrowed
    only_homes = client.get("/", params={"segment": "household"}).text
    assert POINT in only_homes and POINT[:-1] + "B" not in only_homes
    empty = client.get("/", params={"q": "ZZZ"}).text
    assert "No customer matches" in empty


def test_the_search_answers_htmx_with_the_table_alone(client):
    client.cookies.set(SESSION_COOKIE, signed_in_as("sales"))
    partial = client.get("/", params={"q": POINT}, headers={"HX-Request": "true"}).text
    assert "<table>" in partial and "<html" not in partial and "<form" not in partial


def test_the_customer_page_shows_the_typical_day_and_the_settled_days(client):
    client.cookies.set(SESSION_COOKIE, signed_in_as("sales"))
    page = client.get(f"/customers/{POINT}").text
    assert "A typical day" in page and page.count("<rect") == 24 and "3<span" in page


def test_engine_reasons_become_sentences_with_a_band():
    assert main.explained(["premium for unpredictability: usual; about as much as most"]) == [
        ("Premium for unpredictability", "usual", "About as much as most.")
    ]
    assert main.explained(["a line without the shape"]) == [("A line without the shape", "", "")]


def test_the_portfolio_of_the_day_survives_a_restart_through_one_json_file(tmp_path):
    first = Portfolio(cache_dir=tmp_path)
    calls = []

    class CountingEngine(FakeEngine):
        def portfolio(self, prices, customers):
            calls.append(1)
            return super().portfolio(prices, customers)

    first.current(FakeStore(), CountingEngine())
    again = Portfolio(cache_dir=tmp_path)  # a new process
    value = again.current(FakeStore(), CountingEngine())
    assert calls == [1] and value.high_errors == (0.1, 0.2, 0.3) and value.skipped == 1
    assert list(tmp_path.glob("*.json"))[0].read_text("utf-8").startswith("{")


# --- the portfolio page -----------------------------------------------------------------------


class Village(FakeStore):
    """Three households taking 1, 2 and 4 kWh a quarter hour, a business taking 8, and a
    municipality without a settled quarter hour; everyone is registered at twice what we supply."""

    takes = {"H": 1.0, "I": 2.0, "J": 4.0, "K": 8.0, "L": 0.0}
    loads = 0

    def customers(self):
        segments = {"K": "business", "L": "municipality"}
        return [
            Customer(POINT[:-1] + key, segments.get(key, "household"), "H0", take * 70080 or 9.0)
            for key, take in self.takes.items()
        ]

    def settled(self, metering_point):
        Village.loads += 1
        take = self.takes[metering_point[-1]]
        return [(datetime(2025, 1, 1, tzinfo=UTC), 1.0, take)] * 4 if take else []


class Spreading(FakeEngine):
    """A fixed price and a bill that grow with the volume, and a refusal above 200,000 kWh."""

    asked = 0

    def price(self, prices, settled, annual_kwh, spread, contract, others):
        Spreading.asked += 1
        if annual_kwh > 200_000:
            raise ToolError("cannot price this customer: the consumption does not cover a year")
        offer = super().price(prices, settled, annual_kwh, spread, contract, others)
        offer.fixed.eur_kwh = annual_kwh / 350_400
        offer.dynamic.expected_bill_eur = annual_kwh / 100
        return offer


def summary_saying(client, text):
    """Ask for the summary the way the page polls for it, until it says `text`."""
    for _ in range(200):
        answer = client.get("/portfolio/summary", headers=HTMX)
        if text in answer.text:
            return answer
        time.sleep(0.02)
    raise AssertionError(answer.text)


def test_the_day_prices_every_customer_once_and_spreads_them_by_segment(tmp_path):
    Village.loads = Spreading.asked = 0
    first = Portfolio(cache_dir=tmp_path)
    assert first.ready() is None
    first.current(Village(), Spreading())
    first.current(Village(), Spreading())
    assert (Village.loads, Spreading.asked) == (5, 4)  # one load each; no rows, no question
    business, household, municipality = by_segment(first.ready())
    assert household["customers"] == household["priced"] == 3
    assert (household["registered_kwh"], household["supplied_kwh"]) == (490_560, 245_280)
    assert household["share"] == 50
    assert household["fixed"] == pytest.approx((10, 20, 40))
    assert household["bills"] == (350, 701, 1402)
    assert (business["customers"], business["priced"], business["fixed"]) == (1, 0, None)
    assert (municipality["supplied_kwh"], municipality["priced"]) == (0, 0)
    again = Portfolio(cache_dir=tmp_path)  # a new process reads the file, not the warehouse
    assert again.ready() == first.ready() and Village.loads == 5
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_the_portfolio_page_never_waits_and_fills_in_when_the_day_is_ready(cold_client, record):
    client = cold_client
    signed_in_as("sales")
    entered, release, runs = threading.Event(), threading.Event(), []

    class Slow(FakeEngine):
        def portfolio(self, prices, customers):
            runs.append(1)
            entered.set()
            release.wait(10)
            return super().portfolio(prices, customers)

    main.app.dependency_overrides[engine] = Slow
    page = client.get("/portfolio")
    assert page.status_code == 200 and page.text.count("<h1") == 1
    assert 'hx-get="/portfolio/summary" hx-trigger="every 10s"' in page.text
    assert "htmx-indicator" in page.text and "Still calculating" in page.text
    assert entered.wait(10)
    client.get("/portfolio")  # a second view while it runs starts nothing
    waiting = client.get("/portfolio/summary", headers=HTMX)
    assert 'hx-trigger="every 10s"' in waiting.text and "<h1" not in waiting.text
    main.rate_limit = RateLimit(per_hour=1)  # an offer asked meanwhile is free and immediate
    for _ in range(2):
        early = client.post(f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX)
        assert early.status_code == 422 and "still being calculated" in early.text
    release.set()
    done = summary_saying(client, 'id="offers"')
    assert "hx-trigger" not in done.text and 'id="segments"' in done.text and "<svg" in done.text
    assert "<h1" not in done.text and runs == [1]
    events = [row["event"] for _, row in record.rows]
    assert events == ["portfolio", "portfolio", "refused", "refused"]  # never a poll
    assert client.post(
        f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX
    ).is_success


def test_a_failed_calculation_is_one_escaped_line_and_rests_ten_minutes(cold_client, record):
    client = cold_client
    signed_in_as("support")
    runs = []

    class Failing(FakeEngine):
        def portfolio(self, prices, customers):
            runs.append(1)
            raise ToolError("cannot place the portfolio: <b>no history</b>")

    main.app.dependency_overrides[engine] = Failing
    assert "Still calculating" in client.get("/portfolio").text
    failed = summary_saying(client, "Cannot place the portfolio")
    assert failed.status_code == 422 and "&lt;b&gt;no history" in failed.text
    assert "error-card" in failed.text and "hx-trigger" not in failed.text
    refused = [row for _, row in record.rows if row["event"] == "refused"][-1]
    assert refused["detail"] == {
        "reason": "cannot place the portfolio: <b>no history</b>",
        "view": "portfolio",
    }
    warmed()
    again = client.get("/portfolio")  # a reload within ten minutes starts nothing
    assert again.status_code == 422 and "Cannot place the portfolio" in again.text
    assert "hx-trigger" not in again.text and again.text.count("<h1") == 1
    offer = client.post(f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX)
    assert offer.status_code == 422 and "Cannot place the portfolio" in offer.text
    warmed()
    assert runs == [1]
    main.portfolio.failed_at -= timedelta(minutes=11)
    assert "Still calculating" in client.get("/portfolio").text
    warmed()
    assert runs == [1, 1]


def test_the_portfolio_page_shows_segments_and_spreads_and_no_pricing_terms(
    cold_client, monkeypatch
):
    client = cold_client
    signed_in_as("sales")
    monkeypatch.setattr(main, "store", Village)
    main.portfolio.current(Village(), Spreading())
    page = client.get("/portfolio").text
    assert "hx-trigger" not in page and page.count("<h1>Portfolio</h1>") == 1
    for header in ("Share supplied", "Not priced", "Fixed offer", "Expected market-linked bill"):
        assert header in page
    assert '<td class="num">490,560</td><td class="num">245,280</td>' in page
    assert '<td class="num">50 %</td><td class="num">3</td><td class="num">0</td>' in page
    for figure in ("10.00", "20.00", "40.00", "350", "701", "1,402"):
        assert f'<td class="num">{figure}</td>' in page
    assert page.count("no customer priced") == 2
    assert "household 10.00 c/kWh to 40.00 c/kWh, median 20.00 c/kWh" in page
    for word in ("coefficient", "premium", "margin", "confidence"):
        assert word not in page.split("</style>")[1].lower()


def test_the_error_page_has_its_one_heading(client):
    signed_in_as("sales")
    assert client.get("/customers/DROP-TABLE").text.count("<h1") == 1


def test_the_first_offer_of_a_cold_day_starts_the_calculation_and_the_next_one_prices(
    cold_client, record
):
    signed_in_as("sales")
    first = cold_client.post(f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX)
    assert first.status_code == 422 and "still being calculated" in first.text
    assert record.rows[-1][1]["event"] == "refused"
    warmed()
    assert cold_client.post(
        f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX
    ).is_success


def test_an_offer_on_a_warm_day_reads_the_customer_once_and_never_the_spread(client, monkeypatch):
    signed_in_as("sales")
    reads = []

    class Watched(FakeStore):
        def settled(self, metering_point):
            reads.append("settled")
            return super().settled(metering_point)

        def prices(self):
            reads.append("prices")
            return super().prices()

        def spread_eur_kwh(self):
            raise StoreError("the warehouse is not reachable")

    monkeypatch.setattr(main, "store", Watched)
    for contract in main.CONTRACTS:
        reads.clear()
        answer = client.post(f"/customers/{POINT}/offer", data={"contract": contract}, headers=HTMX)
        assert answer.is_success and sorted(reads) == ["prices", "settled"]
    assert main.portfolio.spread == 0.05


def test_a_cache_file_cut_short_is_computed_again_and_written_whole_for_its_owner(tmp_path):
    Portfolio(cache_dir=tmp_path).current(FakeStore(), FakeEngine())
    (path,) = tmp_path.iterdir()
    assert path.stat().st_mode & 0o777 == 0o600
    whole = path.read_text("utf-8")
    for broken in (whole[:40], "[]", whole.replace('"offers"', '"old"')):
        path.write_text(broken, "utf-8")
        again = Portfolio(cache_dir=tmp_path)
        assert again.ready() is None
        assert again.current(FakeStore(), FakeEngine()).skipped == 1
        assert path.read_text("utf-8") == whole and [path] == list(tmp_path.iterdir())
