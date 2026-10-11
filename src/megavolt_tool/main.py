"""The routes. Every one but the sign-in page sits behind the session gate.

Run it with a file that holds nothing but what the tool needs (`.env.*` is gitignored), never
with the platform's whole `.env`:

    uv run --env-file .env.tool uvicorn megavolt_tool.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import html
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from megavolt_tool import session
from megavolt_tool.auth import (
    SESSION_COOKIE,
    User,
    current_user,
    require,
    same_origin_form,
    same_origin_htmx,
)
from megavolt_tool.pricing import Engine, Portfolio, RateLimit, ToolError, engine, shown
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
log = logging.getLogger("megavolt_tool")

templates = Jinja2Templates(directory=HERE / "templates")
app = FastAPI(title="MegaVolt pricing tool", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
rate_limit = RateLimit()
sign_in_limit = RateLimit(per_hour=SIGN_INS_PER_HOUR)
portfolio = Portfolio()


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
    return HTMLResponse(f'<p class="error">{html.escape(str(exc))}</p>', status_code=503)


@app.exception_handler(HTTPException)
def _http_error(request: Request, exc: HTTPException) -> HTMLResponse | RedirectResponse:
    """A browser without a session is sent to the sign-in page; everything else is one line."""
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        if request.method == "GET":
            return RedirectResponse("/signin", status_code=status.HTTP_303_SEE_OTHER)
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse("", status_code=exc.status_code, headers={"HX-Redirect": "/signin"})
    return HTMLResponse(
        f'<p class="error">{html.escape(str(exc.detail))}</p>',
        status_code=exc.status_code,
        headers=exc.headers,
    )


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
def customers(request: Request, user: Annotated[User, Depends(current_user)]):
    """Every customer, to pick one from."""
    rows = store().customers()
    return templates.TemplateResponse(request, "customers.html", {"user": user, "customers": rows})


@protected.get("/customers/{metering_point}", response_class=HTMLResponse)
def customer(request: Request, metering_point: str, user: Annotated[User, Depends(current_user)]):
    """One customer and the contract choice."""
    try:
        found = store().customer(valid_metering_point(metering_point))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not one of our customers")
    return templates.TemplateResponse(
        request, "customer.html", {"user": user, "customer": found, "contracts": CONTRACTS}
    )


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
):
    """The calculation sales asked for, audited and rate limited."""
    if contract not in CONTRACTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown contract")
    try:
        point = valid_metering_point(metering_point)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    try:
        rate_limit.check(user.email)
        found = store().customer(point)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not one of our customers")
        view = shown(
            calculator.price(
                store().prices(),
                store().settled(point),
                found.supplied_kwh,
                store().spread_eur_kwh(),
                contract,
                portfolio.current(store(), calculator),
            )
        )
    except (ToolError, StoreError) as exc:
        audit(user, "refused", customer=point, contract=contract, reason=str(exc))
        return templates.TemplateResponse(
            request, "offer.html", {"user": user, "error": str(exc)}, status_code=422
        )
    audit(user, "priced", customer=point, contract=contract, **view)
    return templates.TemplateResponse(request, "offer.html", {"user": user, "offer": view})


@protected.get("/portfolio", response_class=HTMLResponse)
def portfolio_view(
    request: Request,
    user: Annotated[User, Depends(require("analyst", "admin"))],
    calculator: Annotated[Engine, Depends(engine)],
):
    """The analyst's view: where every customer stands. Filled in a later step."""
    try:
        current = portfolio.current(store(), calculator)
    except (ToolError, StoreError) as exc:
        audit(user, "refused", reason=str(exc), view="portfolio")
        return templates.TemplateResponse(
            request, "portfolio.html", {"user": user, "error": str(exc)}, status_code=422
        )
    audit(user, "portfolio")
    return templates.TemplateResponse(
        request, "portfolio.html", {"user": user, "portfolio": current}
    )


app.include_router(protected)
