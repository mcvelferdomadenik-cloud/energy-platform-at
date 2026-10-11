"""Sessions and the record, against a stand-in Supabase: sign-in, verification, the audit row."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from megavolt_tool import session as sessions
from megavolt_tool.session import Session, SessionError, Settings, Supabase

URL = "https://test-project.supabase.test"
# Invented signing keys, spelled so that no secret scanner mistakes them for real ones.
SECRET = "x" * 48
CONFIG = Settings(URL, "anon-test-key", SECRET)


def token(role="analyst", secret=SECRET, **overrides):
    """A token the way Supabase signs one on the legacy secret, with a role in app_metadata."""
    claims = {
        "aud": "authenticated",
        "iss": f"{URL}/auth/v1",
        "sub": "user-1",
        "email": "a@megavolt.test",
        "exp": int(time.time()) + 3600,
        "app_metadata": {"role": role} if role else {},
    }
    claims.update(overrides)
    return jwt.encode(claims, secret, algorithm="HS256")


def stand_in(handler):
    """A Supabase whose HTTP goes to `handler` instead of the internet."""
    return Supabase(CONFIG, transport=httpx.MockTransport(handler))


# --- sign in and out ------------------------------------------------------------------------------


def test_sign_in_exchanges_the_password_for_a_session():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["apikey"] = request.headers.get("apikey")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600}
        )

    opened = stand_in(handler).sign_in("a@megavolt.test", "pw")
    assert opened == Session("t", "r", 3600)
    assert seen["url"] == f"{URL}/auth/v1/token?grant_type=password"
    assert seen["apikey"] == "anon-test-key"
    assert seen["body"] == {"email": "a@megavolt.test", "password": "pw"}


def test_a_refused_sign_in_says_only_that_it_failed():
    def handler(request):
        return httpx.Response(400, json={"error_description": "Invalid login credentials"})

    with pytest.raises(SessionError, match="^sign-in failed$"):
        stand_in(handler).sign_in("a@megavolt.test", "wrong")


def test_an_unreachable_service_is_a_plain_message_too():
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(SessionError, match="not reachable"):
        stand_in(handler).sign_in("a@megavolt.test", "pw")


# --- verifying a token ----------------------------------------------------------------------------


def test_a_good_token_yields_the_person_and_their_role():
    claims = stand_in(lambda r: httpx.Response(500)).verify(token("sales"))
    assert (claims.user_id, claims.email, claims.role) == ("user-1", "a@megavolt.test", "sales")


@pytest.mark.parametrize(
    "bad",
    [
        token(secret="y" * 48),
        token(exp=int(time.time()) - 10),
        token(aud="anon"),
        token(iss="https://other.supabase.test/auth/v1"),
        "not-a-token",
    ],
)
def test_a_wrong_signature_expiry_audience_or_issuer_is_refused(bad):
    with pytest.raises(SessionError, match="not recognised"):
        stand_in(lambda r: httpx.Response(500)).verify(bad)


def test_a_valid_token_without_a_role_is_refused():
    with pytest.raises(SessionError, match="no role"):
        stand_in(lambda r: httpx.Response(500)).verify(token(role=None))
    with pytest.raises(SessionError, match="no role"):
        stand_in(lambda r: httpx.Response(500)).verify(token(role="superuser"))


def test_a_project_with_published_keys_is_verified_against_them(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    signed = jwt.encode(
        {
            "aud": "authenticated",
            "iss": f"{URL}/auth/v1",
            "sub": "user-2",
            "email": "b@megavolt.test",
            "exp": int(time.time()) + 60,
            "app_metadata": {"role": "admin"},
        },
        private,
        algorithm="ES256",
        headers={"kid": "k1"},
    )

    class Keys:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, value):
            if jwt.get_unverified_header(value).get("kid") != "k1":
                raise jwt.exceptions.PyJWKClientError("no such key")

            class Key:
                key = private.public_key()

            return Key()

    monkeypatch.setattr(sessions, "PyJWKClient", Keys)
    client = Supabase(
        Settings(URL, "anon-test-key", None),
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert client.verify(signed).role == "admin"
    with pytest.raises(SessionError, match="not recognised"):
        client.verify(token("admin"))  # the legacy HS256 token is not accepted by a keyed project
    other_kid = jwt.encode(
        jwt.decode(signed, options={"verify_signature": False}),
        private,
        algorithm="ES256",
        headers={"kid": "k2"},
    )
    with pytest.raises(SessionError, match="not recognised"):
        client.verify(other_kid)
    with pytest.raises(SessionError, match="not recognised"):
        stand_in(lambda r: httpx.Response(500)).verify(signed)  # ES256 against a secret project


def test_a_token_without_an_expiry_or_a_subject_or_with_no_algorithm_is_refused():
    client = stand_in(lambda r: httpx.Response(500))
    no_exp = jwt.encode(
        {
            "aud": "authenticated",
            "iss": f"{URL}/auth/v1",
            "sub": "u",
            "app_metadata": {"role": "sales"},
        },
        SECRET,
        algorithm="HS256",
    )
    no_sub = token(sub=None)
    unsigned = jwt.encode(
        jwt.decode(token(), options={"verify_signature": False}), None, algorithm="none"
    )
    for bad in (no_exp, no_sub, unsigned):
        with pytest.raises(SessionError, match="not recognised"):
            client.verify(bad)


# --- the record -----------------------------------------------------------------------------------


def test_the_record_is_written_as_the_person_with_their_own_token():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["prefer"] = request.headers.get("Prefer")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201)

    stand_in(handler).record("tok", {"email": "a@megavolt.test", "event": "priced"})
    assert seen["url"] == f"{URL}/rest/v1/audit_log"
    assert seen["auth"] == "Bearer tok" and seen["prefer"] == "return=minimal"
    assert seen["body"]["event"] == "priced"


def test_a_record_that_cannot_be_written_is_an_error_not_a_shrug():
    with pytest.raises(SessionError, match="could not be written"):
        stand_in(lambda r: httpx.Response(401)).record("tok", {})
    with pytest.raises(SessionError, match="could not be written"):
        stand_in(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))).record("tok", {})


def test_an_answer_that_is_not_a_session_is_a_refusal_not_a_trace():
    portal = stand_in(lambda r: httpx.Response(200, text="<html>captive portal</html>"))
    with pytest.raises(SessionError, match="sign-in failed"):
        portal.sign_in("a", "b")
    partial = stand_in(lambda r: httpx.Response(200, json={"access_token": "t"}))
    with pytest.raises(SessionError, match="sign-in failed"):
        partial.sign_in("a", "b")


def test_settings_come_from_the_environment_only(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    with pytest.raises(SessionError, match=".env.example"):
        sessions.settings()
    monkeypatch.setenv("SUPABASE_URL", f"{URL}/")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "")
    assert sessions.settings() == Settings(URL, "anon", None)
    monkeypatch.setenv("SUPABASE_URL", "http://test-project.supabase.test")
    with pytest.raises(SessionError, match="https://"):
        sessions.settings()
