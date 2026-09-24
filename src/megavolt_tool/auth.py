"""Who is asking, and what they may do. One gate in front of every route.

Sessions come from Supabase Auth (next step); until then no session is valid, so every protected
route answers 401. Tests replace `current_user` through FastAPI's dependency overrides.
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
    """A signed-in person: an address to audit under, and a role that says what they may see."""

    email: str
    role: Role


def current_user(
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    """The signed-in user behind the request, or 401."""
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in first")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session not recognised")


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
