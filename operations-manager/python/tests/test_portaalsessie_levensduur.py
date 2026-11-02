"""De absolute levensduur van een portaalsessie (RC-287), nu server-side (RC-390).

De guard leest ``login_at`` uit de sessiedata - die is contractueel gelijk aan wat het
was, maar woont sinds RC-390 in de tabel achter het ondertekende sessie-id. Hier wordt de
bovengrens gemeten over de ECHTE middlewares heen, met een sessie in een in-memory store.

De logs-WebSocket leest dezelfde sessie via de middleware en hoort dezelfde grens aan te
houden; daarom staat die handshake hier ook in.
"""

from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from opi.api.logs_websocket_router import logs_websocket_router
from opi.core.auth_decorators import requires_sso
from opi.core.config import settings
from opi.middleware.authorization import AuthorizationMiddleware
from opi.middleware.server_side_session import ServerSideSessionMiddleware
from starlette.websockets import WebSocketDisconnect
from tests.inmemory_session_store import InMemorySessionStore

if TYPE_CHECKING:
    from collections.abc import Iterator

EMAIL = "levensduur@rijksoverheid.nl"
EEN_UUR = 3600


def _cookie(store: InMemorySessionStore, login_at: float | None, email: str = EMAIL) -> str:
    """Een sessie in de store met dit inlogtijdstip; de cookie draagt het ondertekende id."""
    user: dict[str, Any] = {"email": email, "name": "Toets"}
    if login_at is not None:
        user["login_at"] = login_at
    session_id = store.seed(secrets.token_urlsafe(32), {"user": user})
    return TimestampSigner(str(settings.SECRET_KEY)).sign(session_id.encode()).decode()


def _app(store: InMemorySessionStore) -> FastAPI:
    """Een app met alleen de twee middlewares en een route die een sessie eist.

    Dezelfde volgorde als ``create_app``: de sessiemiddleware wordt het laatst toegevoegd
    en zit dus buiten de autorisatie, die de sessie leest.
    """
    app = FastAPI()

    @app.get("/beveiligd")
    @requires_sso
    async def beveiligd(request: Request) -> PlainTextResponse:
        return PlainTextResponse("binnen")

    app.add_middleware(AuthorizationMiddleware)
    app.add_middleware(
        ServerSideSessionMiddleware,
        secret_key=str(settings.SECRET_KEY),
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        https_only=False,
        store=store,
    )
    return app


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture
def client(store: InMemorySessionStore) -> Iterator[TestClient]:
    """De allowlist staat deze gebruiker toe: hier is alleen de levensduur aan de beurt."""
    toegang = MagicMock()
    toegang.is_email_allowed.return_value = True
    with patch("opi.middleware.authorization.get_user_service", return_value=toegang):
        yield TestClient(_app(store))


def test_a_fresh_session_is_allowed_through(client: TestClient, store: InMemorySessionStore) -> None:
    client.cookies.set("session", _cookie(store, time.time() - EEN_UUR))

    antwoord = client.get("/beveiligd", follow_redirects=False)

    assert antwoord.status_code == 200
    assert antwoord.text == "binnen"


def test_a_session_past_its_absolute_lifetime_is_refused(client: TestClient, store: InMemorySessionStore) -> None:
    """Dertien uur na het inloggen, zonder stil te hebben gelegen: toch opnieuw inloggen."""
    client.cookies.set("session", _cookie(store, time.time() - settings.SESSION_ABSOLUTE_MAX_SECONDS - EEN_UUR))

    antwoord = client.get("/beveiligd", follow_redirects=False)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "/auth/login"
    # En de cookie gaat ook in de browser weg, anders wordt hij elk verzoek opnieuw
    # afgekeurd.
    assert "session=null" in antwoord.headers["set-cookie"]
    assert store._rows == {}, "de rij van een geweigerde sessie verdwijnt server-side ook"


def test_a_session_without_a_login_time_is_refused(client: TestClient, store: InMemorySessionStore) -> None:
    """Sessies zonder inlogtijdstip dateren van voor RC-287 en zijn vervallen."""
    client.cookies.set("session", _cookie(store, None))

    antwoord = client.get("/beveiligd", follow_redirects=False)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "/auth/login"


def test_a_session_whose_row_is_gone_is_refused(client: TestClient, store: InMemorySessionStore) -> None:
    """Server-side betekent intrekken: de rij weg. De cookie is geldig ondertekend, maar
    zonder rij is er geen sessie - een harde weigering op de volgende request."""
    session_id = store.seed(secrets.token_urlsafe(32), {"user": {"email": EMAIL, "login_at": time.time()}})
    del store._rows[session_id]
    client.cookies.set("session", TimestampSigner(str(settings.SECRET_KEY)).sign(session_id.encode()).decode())

    antwoord = client.get("/beveiligd", follow_redirects=False)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "/auth/login"


def test_the_logs_websocket_refuses_an_expired_session() -> None:
    """De handshake leest dezelfde sessie via de middleware, dus hij houdt dezelfde grens."""
    store = InMemorySessionStore()
    app = FastAPI()
    app.include_router(logs_websocket_router)
    app.add_middleware(
        ServerSideSessionMiddleware,
        secret_key=str(settings.SECRET_KEY),
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        https_only=False,
        store=store,
    )
    client = TestClient(app)
    client.cookies.set("session", _cookie(store, time.time() - settings.SESSION_ABSOLUTE_MAX_SECONDS - EEN_UUR))

    registreer = AsyncMock(return_value=True)
    with (
        patch("opi.api.logs_websocket_router._register_connection", registreer),
        pytest.raises(WebSocketDisconnect) as weigering,
        client.websocket_connect("/api/logs/stream/psd-law?deployment=pr-1&component=api"),
    ):
        pass

    assert weigering.value.code == 4001
    registreer.assert_not_called()
