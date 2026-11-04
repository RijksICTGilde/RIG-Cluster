"""
Tests for the session cookie and the server-side idle timeout.

The session cookie is a browser-session cookie: it carries no Max-Age and no
expires, so it is gone when the browser closes (RC-415). The idle timeout lives
on the server, on the cookie signature and on the store row, and is sourced from
settings.SESSION_MAX_AGE_SECONDS. Since RC-390 the cookie carries only a signed
session id; the content lives server-side (see tests/test_server_side_session.py).

This module also covers the cookie attributes the ASVS self-assessment leans on
(V3.3.1 secure, V3.3.2 samesite, V3.3.4 httponly).
"""

from datetime import UTC, datetime, timedelta

from freezegun import freeze_time
from itsdangerous import TimestampSigner
from opi.core.config import settings
from opi.middleware.server_side_session import ServerSideSessionMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from tests.inmemory_session_store import InMemorySessionStore

SECRET = "x" * 32


def _app(store: InMemorySessionStore, https_only: bool = False) -> Starlette:
    async def set_session(request):
        request.session["user"] = {"email": "user@example.com"}
        return PlainTextResponse("ok")

    async def read_session(request):
        return PlainTextResponse(request.session.get("user", {}).get("email", ""))

    return Starlette(
        routes=[Route("/set", set_session), Route("/read", read_session)],
        middleware=[
            Middleware(
                ServerSideSessionMiddleware,
                secret_key=SECRET,
                max_age=settings.SESSION_MAX_AGE_SECONDS,
                https_only=https_only,
                store=store,
            )
        ],
    )


def test_session_cookie_is_a_browser_session_cookie():
    """A response that writes to the session emits no Max-Age and no expires."""
    client = TestClient(_app(InMemorySessionStore()))

    set_cookie = client.get("/set").headers["set-cookie"]

    assert "max-age" not in set_cookie.lower()
    assert "expires" not in set_cookie.lower()


def test_session_cookie_keeps_its_security_attributes():
    """ASVS V3.3.2 and V3.3.4: httponly, samesite=lax and path=/ stay on the cookie."""
    client = TestClient(_app(InMemorySessionStore()))

    set_cookie = client.get("/set").headers["set-cookie"].lower()

    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie


def test_session_cookie_is_secure_when_https_only():
    """ASVS V3.3.1: with https_only the cookie carries the Secure attribute."""
    client = TestClient(_app(InMemorySessionStore(), https_only=True))

    set_cookie = client.get("/set").headers["set-cookie"].lower()

    assert "secure" in set_cookie


def test_a_cookie_older_than_the_idle_window_is_refused():
    """The boundary that replaced Max-Age: a valid signature older than the window is rejected.

    The cookie survives in a browser that restores its session, so the server has to
    be the one saying no.
    """
    store = InMemorySessionStore()
    session_id = "session-id-from-a-restored-browser"
    store.seed(session_id, {"user": {"email": "user@example.com"}})
    client = TestClient(_app(store))

    too_old = datetime.now(UTC) - timedelta(seconds=settings.SESSION_MAX_AGE_SECONDS + 60)
    with freeze_time(too_old):
        client.cookies.set("session", TimestampSigner(SECRET).sign(session_id.encode()).decode())

    response = client.get("/read")

    assert response.text == "", "the handler sees no user behind an expired signature"


def test_create_app_wires_session_max_age():
    """create_app() configures the session middleware with the server-side idle timeout.

    Regression guard: the timeout is enforced on the signature and the store row, so it
    has to be wired even though the cookie itself no longer carries a lifetime.
    """
    from opi.server import create_app

    app = create_app()
    session_mw = next(mw for mw in app.user_middleware if mw.cls is ServerSideSessionMiddleware)
    assert session_mw.kwargs["max_age"] == settings.SESSION_MAX_AGE_SECONDS


def test_default_idle_timeout_is_eight_hours():
    """The default session lifetime is one workday (8 hours)."""
    assert settings.SESSION_MAX_AGE_SECONDS == 28800
