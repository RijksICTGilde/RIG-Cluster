"""
Tests for the session cookie idle timeout.

The session cookie must carry a Max-Age so sessions expire after a bounded
period of inactivity instead of living indefinitely. The lifetime is sourced
from settings.SESSION_MAX_AGE_SECONDS and shared between the HTTP
SessionMiddleware and the WebSocket handshake.
"""

from opi.core.config import settings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def test_session_cookie_carries_max_age():
    """A response that writes to the session emits a Max-Age matching the setting."""

    async def set_session(request):
        request.session["user"] = {"email": "user@example.com"}
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/set", set_session)],
        middleware=[Middleware(SessionMiddleware, secret_key="x" * 32, max_age=settings.SESSION_MAX_AGE_SECONDS)],
    )
    client = TestClient(app)

    response = client.get("/set")
    set_cookie = response.headers["set-cookie"]
    assert f"Max-Age={settings.SESSION_MAX_AGE_SECONDS}" in set_cookie


def test_create_app_wires_session_max_age():
    """create_app() configures SessionMiddleware with the shared idle timeout.

    Regression guard: before this was added the cookie had no Max-Age and the
    session never expired server-side.
    """
    from opi.server import create_app

    app = create_app()
    session_mw = next(mw for mw in app.user_middleware if mw.cls is SessionMiddleware)
    assert session_mw.kwargs["max_age"] == settings.SESSION_MAX_AGE_SECONDS


def test_default_idle_timeout_is_eight_hours():
    """The default session lifetime is one workday (8 hours)."""
    assert settings.SESSION_MAX_AGE_SECONDS == 28800
