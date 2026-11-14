"""The routes. Every one but the sign-in page sits behind the session gate.

Run it with a file that holds nothing but what the tool needs (`.env.*` is gitignored), never
with the platform's whole `.env`:

    uv run --env-file .env.tool uvicorn megavolt_tool.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from megavolt_tool import charts, session
from megavolt_tool.auth import (
    ROLES,
    SESSION_COOKIE,
    User,
    current_user,
    require,
    same_origin_form,
    same_origin_htmx,
)
from megavolt_tool.pricing import (
    Engine,
    Portfolio,
    RateLimit,
    ToolError,
    by_segment,
    engine,
    shown,
)
from megavolt_tool.store import Store, StoreError, valid_metering_point

HERE = Path(__file__).parent
CONTRACTS = ("fixed", "dynamic", "both")
# The tool speaks plain http on loopback only, so the cookie cannot be marked Secure; if it ever
# gets a hostname and TLS, this becomes True.
COOKIE_SECURE = False
# The tool answers to these names only, so a page elsewhere cannot rebind them to it.
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
# Sign-in attempts per account per hour: an assumption, generous for a person, tight for a script
# guessing one password. Keyed on the account, not the address, so a script rotating addresses
# against one account is still capped; on a loopback tool every address is the same anyway.
SIGN_INS_PER_HOUR = 10
# The largest annual volume a person may type for an offer, in kWh: an assumption, far above
# any household or small business this supplier serves, and a ceiling on what one request
# can ask of the engine.
MAX_ANNUAL_KWH = 1_000_000
WHOLE_KWH = re.compile(r"[1-9][0-9]{0,6}")
WARMING = "portfolio-warm"
CALCULATING = "the portfolio is still being calculated; try again in a few minutes"
log = logging.getLogger("megavolt_tool")

templates = Jinja2Templates(directory=HERE / "templates")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Warm the portfolio figures in the background so the first calculation never waits."""
    threading.Thread(target=warm_portfolio, name=WARMING, daemon=True).start()
    yield


def warm_portfolio(calculator: Engine | None = None) -> None:
    """Today's portfolio, computed in a background thread; a failure is kept for the portfolio
    page, and the next click or page view computes it again."""
    portfolio.failed = portfolio.failed_at = None
    try:
        portfolio.current(store(), calculator or engine())
    except (ToolError, StoreError) as exc:
        portfolio.failed, portfolio.failed_at = str(exc), datetime.now(UTC)
        log.warning("portfolio not warmed: %s", exc)


app = FastAPI(
    title="MegaVolt pricing tool",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
rate_limit = RateLimit()
sign_in_limit = RateLimit(per_hour=SIGN_INS_PER_HOUR)
portfolio = Portfolio(cache_dir=HERE.parents[1] / "data" / "portfolio")


@lru_cache
def store() -> Store:
    """One store for the process; tests replace this name with a fake."""
    return Store()


def audit(user: User, event: str, **fields: object) -> None:
    """One row per action in the record, written as the person; no row, no answer."""
    row = {
        "email": user.email,
        "role": user.role,
        "event": event,
        "customer": fields.pop("customer", None),
        "contract": fields.pop("contract", None),
        "detail": fields,
    }
    # One log line by construction: json.dumps already escapes control characters, and the
    # explicit replace makes that visible to a reader and to CodeQL (py/log-injection).
    log.info(json.dumps(row).replace("\r", "\\r").replace("\n", "\\n"))
    session.supabase().record(user.token, row)


@app.exception_handler(ToolError)
@app.exception_handler(StoreError)
@app.exception_handler(session.SessionError)
def _refused(request: Request, exc: Exception) -> HTMLResponse:
    """Whatever slips past a route's own handling is one escaped line, never a trace."""
    return error_response(request, str(exc), 503)


def error_card(text: str) -> str:
    """One boxed, escaped line the page can swap in anywhere."""
    return f'<div class="card error-card"><p class="error">{html.escape(text)}</p></div>'


def error_response(
    request: Request, text: str, code: int, headers: dict[str, str] | None = None
) -> HTMLResponse:
    """The card alone for htmx to swap in; a browser opening a page gets it inside the layout."""
    if request.method == "GET" and request.headers.get("HX-Request") != "true":
        return templates.TemplateResponse(
            request, "error.html", {"error": text}, status_code=code, headers=headers
        )
    return HTMLResponse(error_card(text), status_code=code, headers=headers)


def typed_volume(text: str) -> float:
    """The annual volume a person typed, in whole kWh within the bound, else a refusal."""
    text = text.strip()
    if WHOLE_KWH.fullmatch(text) and int(text) <= MAX_ANNUAL_KWH:
        return float(int(text))
    raise ToolError(
        f"the annual volume must be a whole number of kWh between 1 and {MAX_ANNUAL_KWH:,}"
    )


def explained(reasons: list[str]) -> list[tuple[str, str, str]]:
    """An engine reason "topic: band; rest" as (Topic, band, Rest.) for the page; a line that
    does not follow that shape is shown whole."""
    out = []
    for line in reasons:
        topic, _, tail = line.partition(":")
        band, _, rest = tail.partition(";")
        if not tail:
            out.append((line[:1].upper() + line[1:], "", ""))
            continue
        rest = rest.strip()
        out.append(
            (
                topic.strip().capitalize(),
                band.strip(),
                rest[:1].upper() + rest[1:] + ("." if rest else ""),
            )
        )
    return out


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException) -> HTMLResponse | RedirectResponse:
    """A browser without a session is sent to the sign-in page; everything else is one line."""
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        if request.method == "GET":
            return RedirectResponse("/signin", status_code=status.HTTP_303_SEE_OTHER)
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse("", status_code=exc.status_code, headers={"HX-Redirect": "/signin"})
    return error_response(request, str(exc.detail), exc.status_code, exc.headers)


@app.exception_handler(RequestValidationError)
def _bad_form(request: Request, exc: RequestValidationError) -> HTMLResponse:
    """A form that does not fit gets one line back, never its own contents echoed."""
    return HTMLResponse(
        '<p class="error">the form was not filled in as expected</p>',
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


@app.get("/signin", response_class=HTMLResponse)
def signin(request: Request):
    """The one page without a session."""
    return templates.TemplateResponse(request, "signin.html")


@app.post("/signin", dependencies=[Depends(same_origin_form)])
def sign_in(
    request: Request,
    email: Annotated[str, Form(max_length=254)],
    password: Annotated[str, Form(max_length=1024)],
):
    """Exchange the form for a session cookie, or show the form again with one line."""
    try:
        sign_in_limit.check(email.strip().lower())
        opened = session.supabase().sign_in(email, password)
        # An account without a role would sign in and then bounce between pages in silence; its
        # Supabase session is closed again so it does not live on for an hour unused.
        try:
            session.supabase().verify(opened.access_token)
        except session.SessionError:
            session.supabase().sign_out(opened.access_token)
            raise
    except (session.SessionError, ToolError) as exc:
        return templates.TemplateResponse(
            request, "signin.html", {"error": str(exc)}, status_code=401
        )
    answer = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    answer.set_cookie(
        SESSION_COOKIE,
        opened.access_token,
        max_age=opened.expires_in,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
    )
    return answer


protected = APIRouter(dependencies=[Depends(current_user)])


@protected.post("/signout", dependencies=[Depends(same_origin_htmx)])
def sign_out(user: Annotated[User, Depends(current_user)]):
    """End the session on both sides."""
    session.supabase().sign_out(user.token)
    answer = HTMLResponse("", headers={"HX-Redirect": "/signin"})
    answer.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict", secure=COOKIE_SECURE)
    return answer


@protected.get("/", response_class=HTMLResponse)
def customers(
    request: Request, user: Annotated[User, Depends(current_user)], q: str = "", segment: str = ""
):
    """Every customer, to pick one from; typed search and a segment narrow the list."""
    rows = store().customers()
    segments = sorted({row.segment for row in rows})
    needle = q.strip().upper()[:33]
    shown = [
        row
        for row in rows
        if needle in row.metering_point and (not segment or row.segment == segment)
    ]
    partial = request.headers.get("HX-Request") == "true"
    page = "customers_table.html" if partial else "customers.html"
    context = {"user": user, "customers": shown, "total": len(rows), "segments": segments,
               "q": needle, "segment": segment, "partial": partial}  # fmt: skip
    return templates.TemplateResponse(request, page, context)


@protected.get("/customers/{metering_point}", response_class=HTMLResponse)
def customer(request: Request, metering_point: str, user: Annotated[User, Depends(current_user)]):
    """One customer and the contract choice."""
    try:
        found = store().customer(valid_metering_point(metering_point))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not one of our customers")
    settled = store().settled(found.metering_point)
    context = {
        "user": user,
        "customer": found,
        "contracts": CONTRACTS,
        "volume_pattern": WHOLE_KWH.pattern,
        "max_annual_kwh": MAX_ANNUAL_KWH,
        "settled_days": len({when.date() for when, _, _ in settled}),
        "profile": charts.columns([4 * v for v in charts.day_profile(settled)], "kWh")
        if settled
        else None,
    }
    return templates.TemplateResponse(request, "customer.html", context)


@protected.post(
    "/customers/{metering_point}/offer",
    response_class=HTMLResponse,
    dependencies=[Depends(same_origin_htmx)],
)
def offer(
    request: Request,
    metering_point: str,
    contract: Annotated[str, Form()],
    user: Annotated[User, Depends(require("sales", "support", "analyst"))],
    calculator: Annotated[Engine, Depends(engine)],
    annual_kwh: Annotated[str, Form()] = "",
):
    """The calculation sales asked for, audited and rate limited; a typed annual volume
    replaces the one we supply."""
    if contract not in CONTRACTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown contract")
    try:
        point = valid_metering_point(metering_point)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    volume, override = None, bool(annual_kwh.strip())
    try:
        # Before the rate limit: a click that only learns the day is not ready costs nothing.
        waiting = day_not_ready(calculator, start=True)
        if waiting:
            raise ToolError(waiting)
        rate_limit.check(user.email)
        found = store().customer(point)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not one of our customers")
        volume = typed_volume(annual_kwh) if override else found.supplied_kwh
        settled, prices = store().settled(point), store().prices()
        view = shown(
            calculator.price(
                prices,
                settled,
                volume,
                portfolio.spread,
                contract,
                portfolio.current(store(), calculator),
            )
        )
    except (ToolError, StoreError) as exc:
        audit(
            user,
            "refused",
            customer=point,
            contract=contract,
            reason=str(exc),
            annual_kwh=None if volume is None else round(volume),
            volume_override=override,
        )
        return templates.TemplateResponse(
            request, "offer.html", {"user": user, "error": str(exc)}, status_code=422
        )
    audit(
        user,
        "priced",
        customer=point,
        contract=contract,
        annual_kwh=round(volume),
        volume_override=override,
        **view,
    )
    fixed_year = view["fixed"]["eur_kwh"] * volume if "fixed" in view else None
    context = {
        "user": user,
        "offer": view,
        "annual_kwh": volume,
        "volume_override": override,
        "fixed_year": fixed_year or None,
        "reasons": explained(view["reasons"]),
        "bills": None,
        "months": None,
        "months_note": "Months with fewer than 20 settled days are not shown; settled means "
        "readings with a final imbalance price, which arrives about two months after the day.",
    }
    if fixed_year and "dynamic" in view:
        dynamic = view["dynamic"]
        context["bills"] = charts.bars(
            [
                ("fixed price", fixed_year, charts.BLUE, None),
                (
                    "market-linked",
                    dynamic["expected_bill_eur"],
                    charts.ORANGE,
                    dynamic["high_bill_eur"],
                ),
            ],
            "€",
        )
    else:
        if "fixed" in view:
            months = charts.monthly_bills(settled, prices, fixed_eur_kwh=view["fixed"]["eur_kwh"])
            colour = charts.BLUE
        else:
            months = charts.monthly_bills(
                settled, prices, fee_eur_kwh=view["dynamic"]["fee_eur_kwh"]
            )
            colour = charts.ORANGE
        if months:
            labels = [name for name, _ in months]
            context["months"] = charts.columns([eur for _, eur in months], "€", colour, labels)
    return templates.TemplateResponse(request, "offer.html", context)


def day_not_ready(calculator: Engine, start: bool) -> str | None:
    """None when today's portfolio is there; else one line saying why not, never waiting for
    it. With `start` the calculation is set going if none runs and none failed just now."""
    busy = portfolio.busy()
    if portfolio.ready() is not None:
        return None
    if busy:
        return CALCULATING
    if start and not portfolio.cooling():
        # Two requests in the same instant may both start one; the second waits for the
        # portfolio's own lock and finds the day done, so the history is never loaded twice.
        threading.Thread(
            target=warm_portfolio, name=WARMING, args=(calculator,), daemon=True
        ).start()
        return CALCULATING
    return portfolio.failed or "the calculation stopped; open the page again"


def portfolio_state(calculator: Engine, start: bool) -> dict:
    """What the portfolio page can show right now: the segments, nothing yet while the
    calculation runs, or why it stopped."""
    waiting = day_not_ready(calculator, start)
    if waiting:
        return {} if waiting == CALCULATING else {"error": waiting}
    segments = by_segment(portfolio.ready() or [])
    spreads = [(s["segment"], *s["fixed"]) for s in segments if s["fixed"]]
    return {"segments": segments, "chart": charts.ranges(spreads, "c/kWh") if spreads else None}


@protected.get("/portfolio", response_class=HTMLResponse)
def portfolio_view(
    request: Request,
    user: Annotated[User, Depends(require(*ROLES))],
    calculator: Annotated[Engine, Depends(engine)],
):
    """How today's offers spread across the customers, by segment; audited once per view."""
    audit(user, "portfolio")
    context = {"user": user, **portfolio_state(calculator, start=True)}
    return templates.TemplateResponse(
        request, "portfolio.html", context, status_code=422 if "error" in context else 200
    )


@protected.get("/portfolio/summary", response_class=HTMLResponse)
def portfolio_summary(
    request: Request,
    user: Annotated[User, Depends(require(*ROLES))],
    calculator: Annotated[Engine, Depends(engine)],
):
    """The part of the portfolio page that polls until the day's calculation is there."""
    state = portfolio_state(calculator, start=False)
    if "error" in state:
        audit(user, "refused", reason=state["error"], view="portfolio")
    return templates.TemplateResponse(
        request, "portfolio_summary.html", state, status_code=422 if "error" in state else 200
    )


app.include_router(protected)
