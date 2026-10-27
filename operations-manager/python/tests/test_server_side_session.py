"""The server-side session middleware (RC-390) over HTTP, with an in-memory store.

The cookie now says only "which session" (a signed id): small and constant in size.
The content lives in the store, and a mutation is visible to a next reader immediately
via write-through.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import opi.api.auth_routes as auth_routes_module
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from freezegun import freeze_time
from itsdangerous import TimestampSigner
from opi.api.auth_routes import auth_router
from opi.middleware.server_side_session import ServerSideSession, ServerSideSessionMiddleware
from tests.inmemory_session_store import InMemorySessionStore

SECRET = "middleware-test-secret"
MAX_AGE = 3600


def _app(store: InMemorySessionStore | _TouchCounter) -> FastAPI:
    app = FastAPI()

    @app.get("/read")
    async def read(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(request.session.get("v", "")))

    @app.get("/write")
    async def write(request: Request) -> PlainTextResponse:
        request.session["v"] = "written"
        return PlainTextResponse("ok")

    @app.get("/empty")
    async def empty(request: Request) -> PlainTextResponse:
        request.session.clear()
        return PlainTextResponse("ok")

    @app.get("/privilege")
    async def privilege(request: Request) -> PlainTextResponse:
        """As auth_callback does: rotate the session id at the privilege hinge."""
        session = request.session
        if isinstance(session, ServerSideSession):
            await session.regenerate()
        request.session["v"] = "privileged"
        return PlainTextResponse("ok")

    @app.get("/rotate-only")
    async def rotate_only(request: Request) -> PlainTextResponse:
        """Rotate at the hinge without a mutation after it."""
        session = request.session
        if isinstance(session, ServerSideSession):
            await session.regenerate()
        return PlainTextResponse("ok")

    @app.get("/write-then-rotate")
    async def write_then_rotate(request: Request) -> PlainTextResponse:
        """A mutation before the rotation is still in flight when the id is killed."""
        request.session["v"] = "changed"
        session = request.session
        if isinstance(session, ServerSideSession):
            await session.regenerate()
        return PlainTextResponse("ok")

    @app.get("/static/image.png")
    async def static(request: Request) -> PlainTextResponse:
        return PlainTextResponse("image")

    # StaticFiles itself is not the subject; the path only has to be an excluded path
    # for the middleware.
    app.add_middleware(
        ServerSideSessionMiddleware,
        secret_key=SECRET,
        max_age=MAX_AGE,
        https_only=False,
        store=store,
    )
    return app


def _sign(session_id: str) -> str:
    """A cookie as the middleware writes it: only the signed id."""
    return TimestampSigner(SECRET).sign(session_id.encode()).decode()


class _TouchCounter:
    """A store that counts its touches, for the sliding-window test."""

    def __init__(self, inner: InMemorySessionStore) -> None:
        self._inner = inner
        self.touches = 0

    async def get(self, session_id: str) -> dict[str, Any] | None:
        return await self._inner.get(session_id)

    async def set(self, session_id: str, data: dict[str, Any]) -> None:
        await self._inner.set(session_id, data)

    async def delete(self, session_id: str) -> None:
        await self._inner.delete(session_id)

    async def touch(self, session_id: str) -> bool:
        self.touches += 1
        return await self._inner.touch(session_id)


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def client(store: InMemorySessionStore) -> TestClient:
    return TestClient(_app(store))


def test_the_cookie_carries_only_a_signed_id(client: TestClient, store: InMemorySessionStore) -> None:
    response = client.get("/write")

    cookie = response.cookies["session"]
    signed = TimestampSigner(SECRET).unsign(cookie, max_age=MAX_AGE).decode()
    assert len(cookie) < 160, "the cookie stays small whatever the session holds"
    assert store._rows[signed] == {"v": "written"}, "the content lives in the store"


def test_the_cookie_has_max_age_httponly_and_path(client: TestClient) -> None:
    response = client.get("/write")

    header = response.headers["set-cookie"]
    assert f"Max-Age={MAX_AGE}" in header
    assert "httponly" in header
    assert "path=/" in header


def test_the_next_reader_sees_the_mutation(client: TestClient) -> None:
    client.get("/write")

    response = client.get("/read")

    assert response.text == "written"


def test_a_heavy_session_keeps_the_cookie_small(client: TestClient, store: InMemorySessionStore) -> None:
    """The whole point of RC-390: whatever lands in the session, the cookie is as big."""
    first = client.get("/write")
    cookie = first.cookies["session"]
    sid = TimestampSigner(SECRET).unsign(cookie, max_age=MAX_AGE).decode()
    store._rows[sid]["id_token"] = "x" * 4000  # heavier than a browser cookie allows

    response = client.get("/read")

    assert response.text == "written", "the content survives outside the cookie"
    assert len(cookie) < 160


def test_a_mutation_resigns_the_same_id(client: TestClient) -> None:
    cookie = client.get("/write").cookies["session"]

    response = client.get("/write")

    fresh = response.cookies["session"]
    assert TimestampSigner(SECRET).unsign(fresh, max_age=MAX_AGE).decode() == (
        TimestampSigner(SECRET).unsign(cookie, max_age=MAX_AGE).decode()
    )


def test_clearing_removes_the_row_and_expires_the_cookie(client: TestClient, store: InMemorySessionStore) -> None:
    client.get("/write")
    assert len(store._rows) == 1

    response = client.get("/empty")

    assert store._rows == {}
    header = response.headers["set-cookie"]
    assert "session=null" in header
    assert "expires=Thu, 01 Jan 1970" in header


def test_regenerating_at_login_kills_the_planted_id(client: TestClient, store: InMemorySessionStore) -> None:
    """Session fixation: an id an attacker planted is dead after the login rotates it."""
    planted = secrets.token_urlsafe(32)
    store.seed(planted, {"oauth_state": "planted"})
    client.cookies.set("session", _sign(planted))

    response = client.get("/privilege")

    assert planted not in store._rows, "the row behind the planted id is gone"
    fresh = TimestampSigner(SECRET).unsign(response.cookies["session"], max_age=MAX_AGE).decode()
    assert fresh != planted, "the browser gets a new id"
    assert store._rows[fresh]["v"] == "privileged", "the content moved along to the new id"

    # The attacker's copy of the planted id buys nothing anymore.
    client.cookies.clear()
    client.cookies.set("session", _sign(planted))
    assert client.get("/read").text == ""


def test_regenerating_an_anonymous_session_mints_a_fresh_id(client: TestClient, store: InMemorySessionStore) -> None:
    """No prior cookie: the delete of the unknown id is a no-op, the new id persists."""
    response = client.get("/privilege")

    sid = TimestampSigner(SECRET).unsign(response.cookies["session"], max_age=MAX_AGE).decode()
    assert store._rows[sid]["v"] == "privileged"


def test_regeneration_alone_carries_the_content_to_the_new_id(client: TestClient, store: InMemorySessionStore) -> None:
    """No mutation after the rotation: the new id still reaches the store and the cookie."""
    planted = secrets.token_urlsafe(32)
    store.seed(planted, {"oauth_state": "planted"})
    client.cookies.set("session", _sign(planted))

    response = client.get("/rotate-only")

    assert planted not in store._rows, "the row behind the planted id is gone"
    assert "session" in response.cookies, "rotation without a later mutation still renews the cookie"
    fresh = TimestampSigner(SECRET).unsign(response.cookies["session"], max_age=MAX_AGE).decode()
    assert fresh != planted
    assert store._rows[fresh] == {"oauth_state": "planted"}, "the content moved along to the new id"


def test_a_pending_write_cannot_resurrect_the_killed_id(client: TestClient, store: InMemorySessionStore) -> None:
    """regenerate flushes before it deletes: a write still in flight to the planted id lands first."""
    planted = secrets.token_urlsafe(32)
    store.seed(planted, {"oauth_state": "planted"})
    client.cookies.set("session", _sign(planted))

    response = client.get("/write-then-rotate")

    assert planted not in store._rows, "the killed id stays dead, the in-flight write may not bring it back"
    fresh = TimestampSigner(SECRET).unsign(response.cookies["session"], max_age=MAX_AGE).decode()
    assert store._rows[fresh] == {"oauth_state": "planted", "v": "changed"}


def _callback_app(store: InMemorySessionStore) -> FastAPI:
    """The real auth_router behind the session middleware, with the OAuth exchange stubbed."""
    app = FastAPI()
    app.include_router(auth_router)
    keycloak = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={"userinfo": {"sub": "sub-1", "email": "dev@example.org", "email_verified": True}}
        )
    )
    app.state.oauth = SimpleNamespace(keycloak=keycloak)
    app.add_middleware(
        ServerSideSessionMiddleware,
        secret_key=SECRET,
        max_age=MAX_AGE,
        https_only=False,
        store=store,
    )
    return app


def test_the_login_callback_rotates_the_session_id(
    store: InMemorySessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixation guard lives in auth_callback itself: the planted id is dead once the login lands."""
    store_user = Mock()
    monkeypatch.setattr(auth_routes_module, "get_user_service", lambda: SimpleNamespace(store_user=store_user))
    client = TestClient(_callback_app(store))
    planted = secrets.token_urlsafe(32)
    store.seed(planted, {"oauth_state": "planted"})
    client.cookies.set("session", _sign(planted))

    response = client.get("/auth/callback", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    assert planted not in store._rows, "the planted id is dead before the identity lands"
    fresh = TimestampSigner(SECRET).unsign(response.cookies["session"], max_age=MAX_AGE).decode()
    assert fresh != planted, "the browser gets a new id"
    assert store._rows[fresh]["user"]["email"] == "dev@example.org", "the identity landed on the new id"
    store_user.assert_called_once()


def test_a_tampered_cookie_is_an_empty_session(client: TestClient) -> None:
    client.cookies.set("session", "tampered")

    response = client.get("/read")

    assert response.status_code == 200
    assert response.text == ""


def test_an_unknown_id_is_an_empty_session(client: TestClient) -> None:
    """A validly signed cookie whose row is gone (purged or revoked)."""
    client.cookies.set("session", _sign(secrets.token_urlsafe(32)))

    response = client.get("/read")

    assert response.status_code == 200
    assert response.text == ""


def test_an_expired_signature_does_not_count(client: TestClient, store: InMemorySessionStore) -> None:
    """The idle window on the signature: older than max_age is a new session."""
    sid = secrets.token_urlsafe(32)
    store.seed(sid, {"v": "old"})
    older_than_the_window = datetime.now(UTC) - timedelta(seconds=MAX_AGE + 60)
    with freeze_time(older_than_the_window):
        client.cookies.set("session", _sign(sid))

    response = client.get("/read")

    assert response.text == "", "an expired cookie does not pick its old content back up"


def test_a_static_path_does_not_read_the_store(client: TestClient, store: InMemorySessionStore) -> None:
    """Static paths and probes touch neither the cache nor the database."""
    sid = secrets.token_urlsafe(32)
    store.seed(sid, {"v": "something"})
    client.cookies.set("session", _sign(sid))
    reads_before = store.reads

    response = client.get("/static/image.png")

    assert response.status_code == 200
    assert store.reads == reads_before


def test_read_only_sends_no_new_cookie(client: TestClient) -> None:
    """No mutation, nothing to renew: the response leaves the cookie alone."""
    client.get("/write")

    response = client.get("/read")

    assert "set-cookie" not in response.headers


def test_no_cookie_for_an_empty_anonymous_session(client: TestClient) -> None:
    """Who writes nothing gets nothing: no cookie, no row (as with Starlette)."""
    response = client.get("/read")

    assert "set-cookie" not in response.headers


def test_https_only_sets_the_secure_flag() -> None:
    store = InMemorySessionStore()
    app = FastAPI()

    @app.get("/write")
    async def write(request: Request) -> PlainTextResponse:
        request.session["v"] = "x"
        return PlainTextResponse("ok")

    app.add_middleware(ServerSideSessionMiddleware, secret_key=SECRET, max_age=MAX_AGE, https_only=True, store=store)

    header = TestClient(app).get("/write").headers["set-cookie"]
    assert "; secure" in header


def test_write_through_from_a_sync_route() -> None:
    """A sync route runs on a threadpool; the write-through has to work from there too."""
    store = InMemorySessionStore()
    app = FastAPI()

    @app.get("/sync-write")
    def sync_write(request: Request) -> PlainTextResponse:
        request.session["v"] = "from-thread"
        return PlainTextResponse("ok")

    app.add_middleware(ServerSideSessionMiddleware, secret_key=SECRET, max_age=MAX_AGE, https_only=False, store=store)

    response = TestClient(app).get("/sync-write")

    assert response.status_code == 200
    assert list(store._rows.values()) == [{"v": "from-thread"}]


def test_an_existing_session_is_touched() -> None:
    """The sliding window of the row moves along with activity."""
    store = InMemorySessionStore()
    counter = _TouchCounter(store)
    sid = secrets.token_urlsafe(32)
    store.seed(sid, {"v": "something"})
    client = TestClient(_app(counter))
    client.cookies.set("session", _sign(sid))

    response = client.get("/read")

    assert response.status_code == 200
    assert counter.touches == 1


def test_a_read_session_marks_the_response_vary_cookie(client: TestClient) -> None:
    """Starlette parity: a response that read the session varies on the Cookie header."""
    client.get("/write")

    response = client.get("/read")

    assert "Cookie" in response.headers["vary"]


class _FailingStore(InMemorySessionStore):
    """A store whose every persist fails (the database is gone)."""

    async def set(self, session_id: str, data: dict[str, Any]) -> None:
        raise RuntimeError("no database")


def test_a_failing_write_through_still_answers_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """A session that cannot be persisted does not break the response; the failure is logged."""
    import logging

    client = TestClient(_app(_FailingStore()))
    with caplog.at_level(logging.ERROR, logger="opi.middleware.server_side_session"):
        response = client.get("/write")

    assert response.status_code == 200
    assert "Persisting session" in caplog.text
    sid = TimestampSigner(SECRET).unsign(response.cookies["session"], max_age=MAX_AGE).decode()
    assert sid not in caplog.text, "the full session id is a bearer and stays out of the log"
    assert sid[:12] in caplog.text, "a prefix still identifies the failing session"
