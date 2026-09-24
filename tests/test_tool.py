"""The tool's routes: nothing without a session, roles, input shape, CSRF, the rate limit.

No warehouse and no pricing engine: both are stood in for, so these run in CI. What the engine
computes is its own tests' business.
"""

import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from megavolt_tool import main, session
from megavolt_tool.auth import SESSION_COOKIE, User, current_user
from megavolt_tool.pricing import RateLimit, ToolError, engine, shown
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
            Customer(POINT, "household", "H0", 3500.0),
            Customer(POINT[:-1] + "B", "business", "G0", 6000.0),
        ]

    def customer(self, metering_point):
        valid_metering_point(metering_point)
        return next((c for c in self.customers() if c.metering_point == metering_point), None)

    def settled(self, metering_point):
        return [(datetime(2025, 1, 1, tzinfo=UTC), 1.0, 1.0)]

    def prices(self):
        return [(datetime(2025, 1, 1, tzinfo=UTC), 100.0)]

    def spread_eur_kwh(self):
        return 0.05


class FakeSupabase:
    """Signs in one person, verifies one token, and keeps every audit row it is given."""

    def __init__(self):
        self.rows = []
        self.signed_out = []

    def sign_in(self, email, password):
        if password != "right":
            raise SessionError("sign-in failed")
        return Session("token-of-" + email.split("@")[0], "refresh", 3600)

    def sign_out(self, access_token):
        self.signed_out.append(access_token)

    def verify(self, access_token):
        raise SessionError("session not recognised")

    def record(self, access_token, row):
        self.rows.append((access_token, row))


class FakeEngine:
    """Answers like the private engine would, without any of its arithmetic."""

    def price(self, prices, settled, annual_kwh, spread, contract, others):
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
def client(monkeypatch, record):
    monkeypatch.setattr(main, "store", FakeStore)
    monkeypatch.setattr(main, "rate_limit", RateLimit())
    monkeypatch.setattr(session, "supabase", lambda: record)
    main.portfolio.reset()
    main.app.dependency_overrides[engine] = FakeEngine
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


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


def test_a_session_cookie_that_does_not_verify_is_refused(client):
    client.cookies.set(SESSION_COOKIE, "stale")
    assert client.post("/signout", headers=HTMX).status_code == 401


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


def test_signing_in_from_another_origin_is_refused(client):
    elsewhere = {"Origin": "http://evil.example"}
    answer = client.post(
        "/signin", data={"email": "a@megavolt.test", "password": "right"}, headers=elsewhere
    )
    assert answer.status_code == 403


def test_signing_out_ends_the_session_on_both_sides(client, record):
    signed_in_as("sales")
    answer = client.post("/signout", headers=HTMX)
    assert answer.status_code == 200 and answer.headers["HX-Redirect"] == "/signin"
    assert f'{SESSION_COOKIE}=""' in answer.headers["set-cookie"]
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
    result = client.post(f"/customers/{POINT}/offer", data={"contract": "both"}, headers=HTMX)
    assert result.status_code == 200
    assert "12.35 c/kWh" in result.text and "391 €" in result.text and "591 €" in result.text
    assert "premium for unpredictability" in result.text
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


def test_sales_may_not_open_the_portfolio(client):
    signed_in_as("sales")
    assert client.get("/portfolio").status_code == 403


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
    assert refused.status_code == 422 and "too many calculations" in refused.text


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


def test_a_missing_engine_and_a_missing_warehouse_are_never_a_500(client, monkeypatch):
    signed_in_as("analyst")
    main.app.dependency_overrides.pop(engine)  # the real wrapper, without the private package
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
