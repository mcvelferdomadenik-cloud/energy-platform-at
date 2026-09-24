"""The routes. Every one but the sign-in page sits behind the session gate.

Run it with a file that holds nothing but the reader's connection string (`.env.*` is
gitignored), never with the platform's whole `.env`:

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
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from megavolt_tool.auth import User, current_user, require, same_origin_htmx
from megavolt_tool.pricing import Engine, Portfolio, RateLimit, ToolError, engine, shown
from megavolt_tool.store import Store, StoreError, valid_metering_point

HERE = Path(__file__).parent
CONTRACTS = ("fixed", "dynamic", "both")
log = logging.getLogger("megavolt_tool")

templates = Jinja2Templates(directory=HERE / "templates")
app = FastAPI(title="MegaVolt pricing tool", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
rate_limit = RateLimit()
portfolio = Portfolio()


@lru_cache
def store() -> Store:
    """One store for the process; tests replace this name with a fake."""
    return Store()


def audit(user: User, event: str, **fields: object) -> None:
    """One line per calculation: who, when, what. The Supabase table replaces this next step."""
    log.info(json.dumps({"user": user.email, "role": user.role, "event": event, **fields}))


@app.exception_handler(ToolError)
@app.exception_handler(StoreError)
def _refused(request: Request, exc: Exception) -> HTMLResponse:
    """Whatever slips past a route's own handling is one escaped line, never a trace."""
    return HTMLResponse(f'<p class="error">{html.escape(str(exc))}</p>', status_code=503)


@app.get("/signin", response_class=HTMLResponse)
def signin(request: Request):
    """The one page without a session."""
    return templates.TemplateResponse(request, "signin.html")


protected = APIRouter(dependencies=[Depends(current_user)])


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
                found.annual_kwh,
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
        audit(user, "refused", event_of="portfolio", reason=str(exc))
        return templates.TemplateResponse(
            request, "portfolio.html", {"user": user, "error": str(exc)}, status_code=422
        )
    audit(user, "portfolio")
    return templates.TemplateResponse(
        request, "portfolio.html", {"user": user, "portfolio": current}
    )


app.include_router(protected)
