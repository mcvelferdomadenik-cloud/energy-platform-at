"""Who is signed in, and the record of what they did: Supabase Auth and one Supabase table.

The tool never holds a key that bypasses row-level security. It signs people in with the public
`anon` key, verifies their access token itself against the project's signing keys, and writes each
audit row with the person's own token, under a policy that allows inserting and nothing else. So
even a broken tool cannot read what it should not or rewrite the record. A session lives as long
as the access token (an hour by default) and is not refreshed: it ends, and the person signs in
again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from megavolt_tool.auth import ROLES

URL_ENV, ANON_ENV, SECRET_ENV = "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_JWT_SECRET"
AUDIENCE = "authenticated"
TIMEOUT_SECONDS = 10.0
AUDIT_TABLE = "audit_log"
# A token without an expiry would never expire; one without a subject names nobody.
REQUIRED_CLAIMS = ["exp", "sub"]


class SessionError(RuntimeError):
    """Raised when a person cannot be signed in, verified or recorded."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Where the project is and how its tokens are signed."""

    url: str
    anon_key: str
    # Set only for a project still on the legacy HS256 secret; new projects publish JWKS.
    jwt_secret: str | None = None


@dataclass(frozen=True, slots=True)
class Session:
    """What Supabase hands back on sign-in."""

    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class Claims:
    """What a verified token says about the person."""

    user_id: str
    email: str
    role: str


def settings() -> Settings:
    """The Supabase settings, from the environment only."""
    url = os.environ.get(URL_ENV, "").strip().rstrip("/")
    anon = os.environ.get(ANON_ENV, "").strip()
    if not url or not anon:
        raise SessionError(f"{URL_ENV} and {ANON_ENV} must be set; see .env.example")
    if not url.startswith("https://"):
        raise SessionError(f"{URL_ENV} must start with https://")
    return Settings(url, anon, os.environ.get(SECRET_ENV, "").strip() or None)


class Supabase:
    """The three calls the tool makes: sign in, sign out, record; and the local verification."""

    def __init__(self, config: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._config = config
        self._http = httpx.Client(
            base_url=config.url,
            headers={"apikey": config.anon_key},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        self._keys = PyJWKClient(f"{config.url}/auth/v1/.well-known/jwks.json", cache_keys=True)

    def sign_in(self, email: str, password: str) -> Session:
        """Exchange an email and password for a session; a refusal never says which was wrong."""
        try:
            answer = self._http.post(
                "/auth/v1/token",
                params={"grant_type": "password"},
                json={"email": email, "password": password},
            )
        except httpx.HTTPError as exc:
            raise SessionError("the sign-in service is not reachable") from exc
        if answer.status_code != 200:
            raise SessionError("sign-in failed")
        try:
            body = answer.json()
            return Session(
                str(body["access_token"]), str(body["refresh_token"]), int(body["expires_in"])
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise SessionError("sign-in failed") from exc

    def sign_out(self, access_token: str) -> None:
        """End the session server-side; a token that is already dead is not an error."""
        try:
            self._http.post("/auth/v1/logout", headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError:
            pass

    def verify(self, access_token: str) -> Claims:
        """Check the token's signature, audience, issuer and expiry, and read the role."""
        issuer = f"{self._config.url}/auth/v1"
        options = {"require": REQUIRED_CLAIMS}
        try:
            if self._config.jwt_secret:
                payload = jwt.decode(
                    access_token,
                    self._config.jwt_secret,
                    algorithms=["HS256"],
                    audience=AUDIENCE,
                    issuer=issuer,
                    options=options,
                )
            else:
                key = self._keys.get_signing_key_from_jwt(access_token).key
                payload = jwt.decode(
                    access_token,
                    key,
                    algorithms=["ES256", "RS256"],
                    audience=AUDIENCE,
                    issuer=issuer,
                    options=options,
                )
        except jwt.PyJWTError as exc:
            raise SessionError("session not recognised") from exc
        role = (payload.get("app_metadata") or {}).get("role")
        if role not in ROLES:
            raise SessionError("this account has no role in the tool")
        return Claims(str(payload["sub"]), str(payload.get("email", "")), role)

    def record(self, access_token: str, row: dict[str, Any]) -> None:
        """Write one audit row as the person themselves; the policy lets them insert and no more."""
        try:
            answer = self._http.post(
                f"/rest/v1/{AUDIT_TABLE}",
                headers={"Authorization": f"Bearer {access_token}", "Prefer": "return=minimal"},
                json=row,
            )
        except httpx.HTTPError as exc:
            raise SessionError("the record could not be written") from exc
        if answer.status_code >= 300:
            raise SessionError("the record could not be written")


@lru_cache
def supabase() -> Supabase:
    """One client for the process; tests replace this name with a fake."""
    return Supabase(settings())
