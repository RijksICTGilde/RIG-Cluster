"""
Tests for the session cookie idle timeout.

The session cookie must carry a Max-Age so sessions expire after a bounded
period of inactivity instead of living indefinitely. The lifetime is sourced
from settings.SESSION_MAX_AGE_SECONDS. Since RC-390 the cookie carries only a
signed session id; the content lives server-side (see tests/test_server_side_session.py).
"""

from opi.core.config import settings
from opi.middleware.server_side_session import ServerSideSessionMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from tests.inmemory_session_store import InMemorySessionStore


def test_session_cookie_carries_max_age():
    """A response that writes to the session emits a Max-Age matching the setting."""

    async def set_session(request):
        request.session["user"] = {"email": "user@example.com"}
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/set", set_session)],
        middleware=[
            Middleware(
                ServerSideSessionMiddleware,
                secret_key="x" * 32,
                max_age=settings.SESSION_MAX_AGE_SECONDS,
                store=InMemorySessionStore(),
            )
        ],
    )
    client = TestClient(app)

    response = client.get("/set")
    set_cookie = response.headers["set-cookie"]
    assert f"Max-Age={settings.SESSION_MAX_AGE_SECONDS}" in set_cookie


def test_create_app_wires_session_max_age():
    """create_app() configures the session middleware with the shared idle timeout.

    Regression guard: before this was added the cookie had no Max-Age and the
    session never expired server-side.
    """
    from opi.server import create_app

    app = create_app()
    session_mw = next(mw for mw in app.user_middleware if mw.cls is ServerSideSessionMiddleware)
    assert session_mw.kwargs["max_age"] == settings.SESSION_MAX_AGE_SECONDS


def test_default_idle_timeout_is_eight_hours():
    """The default session lifetime is one workday (8 hours)."""
    assert settings.SESSION_MAX_AGE_SECONDS == 28800
