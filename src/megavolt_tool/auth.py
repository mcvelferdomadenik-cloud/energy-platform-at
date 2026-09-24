"""Who is asking, and what they may do. One gate in front of every route.

The session is a Supabase access token in an HttpOnly cookie; the gate verifies it on every
request and turns it into a user with a role. Tests replace `current_user` through FastAPI's
dependency overrides.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, HTTPException, Request, status

Role = Literal["sales", "support", "analyst", "admin"]
ROLES: tuple[Role, ...] = ("sales", "support", "analyst", "admin")
SESSION_COOKIE = "megavolt_session"


@dataclass(frozen=True, slots=True)
class User:
    """A signed-in person: an address to audit under, a role, and the token that proves both."""

    email: str
    role: Role
    token: str = ""


def current_user(
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    """The signed-in user behind the request, or 401."""
    from megavolt_tool.session import SessionError, supabase

    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    try:
        claims = supabase().verify(session)
    except SessionError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return User(claims.email, claims.role, session)


def require(*roles: Role) -> Callable[..., User]:
    """A dependency that lets only the given roles through, with 403 for everyone else."""

    def allowed(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not for this role")
        return user

    return allowed


def same_origin_htmx(request: Request) -> None:
    """Refuse a state-changing request that did not come from our own page through htmx (CSRF).

    The custom header is what stops a cross-site form; the origin check is the second lock, and
    it compares scheme and host exactly, never a prefix.
    """
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not an htmx request")
    origin = request.headers.get("Origin") or request.headers.get("Referer", "")
    if urlsplit(origin)[:2] != urlsplit(str(request.base_url))[:2]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "request from another origin")


def same_origin_form(request: Request) -> None:
    """The sign-in form is a plain POST, not htmx: the origin check alone, exact as above."""
    origin = request.headers.get("Origin") or request.headers.get("Referer", "")
    if urlsplit(origin)[:2] != urlsplit(str(request.base_url))[:2]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "request from another origin")
