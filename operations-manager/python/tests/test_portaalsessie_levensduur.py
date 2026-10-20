"""De absolute levensduur van een portaalsessie (RC-287).

De cookie wordt bij elk antwoord opnieuw getekend, dus ``SESSION_MAX_AGE_SECONDS`` is een
schuivend venster: een sessie die in gebruik blijft verliep nooit. Hier wordt de
bovengrens gemeten die daar overheen ligt, over de ECHTE middlewares heen, met een cookie
die net zo is ondertekend als Starlette hem ondertekent.

De logs-WebSocket leest dezelfde cookie en hoort dezelfde grens aan te houden; daarom
staat die handshake hier ook in.
"""

from __future__ import annotations

import base64
import json
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
from starlette.middleware.sessions import SessionMiddleware
from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from collections.abc import Iterator

EMAIL = "levensduur@rijksoverheid.nl"
EEN_UUR = 3600


def _cookie(login_at: float | None, email: str = EMAIL) -> str:
    """Een sessiecookie zoals SessionMiddleware hem tekent, met dit inlogtijdstip."""
    user: dict[str, Any] = {"email": email, "name": "Toets"}
    if login_at is not None:
        user["login_at"] = login_at
    payload = base64.b64encode(json.dumps({"user": user}).encode()).decode()
    return TimestampSigner(str(settings.SECRET_KEY)).sign(payload).decode()


def _app() -> FastAPI:
    """Een app met alleen de twee middlewares en een route die een sessie eist.

    Dezelfde volgorde als ``create_app``: SessionMiddleware wordt het laatst toegevoegd en
    zit dus buiten de autorisatie, die de sessie leest.
    """
    app = FastAPI()

    @app.get("/beveiligd")
    @requires_sso
    async def beveiligd(request: Request) -> PlainTextResponse:
        return PlainTextResponse("binnen")

    app.add_middleware(AuthorizationMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=str(settings.SECRET_KEY))
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """De allowlist staat deze gebruiker toe: hier is alleen de levensduur aan de beurt."""
    toegang = MagicMock()
    toegang.is_email_allowed.return_value = True
    with patch("opi.middleware.authorization.get_user_service", return_value=toegang):
        yield TestClient(_app())


def test_een_verse_sessie_komt_door(client: TestClient) -> None:
    client.cookies.set("session", _cookie(time.time() - EEN_UUR))

    antwoord = client.get("/beveiligd", follow_redirects=False)

    assert antwoord.status_code == 200
    assert antwoord.text == "binnen"


def test_een_sessie_voorbij_de_absolute_levensduur_wordt_geweigerd(client: TestClient) -> None:
    """Dertien uur na het inloggen, zonder stil te hebben gelegen: toch opnieuw inloggen."""
    client.cookies.set("session", _cookie(time.time() - settings.SESSION_ABSOLUTE_MAX_SECONDS - EEN_UUR))

    antwoord = client.get("/beveiligd", follow_redirects=False)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "/auth/login"
    # En de cookie gaat ook in de browser weg, anders wordt hij elk verzoek opnieuw
    # afgekeurd.
    assert "session=null" in antwoord.headers["set-cookie"]


def test_een_sessie_zonder_inlogtijdstip_wordt_geweigerd(client: TestClient) -> None:
    """De sessies van voor deze uitrol dragen geen login_at en vervallen daarmee."""
    client.cookies.set("session", _cookie(None))

    antwoord = client.get("/beveiligd", follow_redirects=False)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "/auth/login"


def test_de_logs_websocket_weigert_een_verlopen_sessie() -> None:
    """De handshake leest dezelfde cookie, dus hij hoort dezelfde grens aan te houden."""
    app = FastAPI()
    app.include_router(logs_websocket_router)
    client = TestClient(app)
    client.cookies.set("session", _cookie(time.time() - settings.SESSION_ABSOLUTE_MAX_SECONDS - EEN_UUR))

    registreer = AsyncMock(return_value=True)
    with (
        patch("opi.api.logs_websocket_router._register_connection", registreer),
        pytest.raises(WebSocketDisconnect) as weigering,
        client.websocket_connect("/api/logs/stream/psd-law?deployment=pr-1&component=api"),
    ):
        pass

    assert weigering.value.code == 4001
    registreer.assert_not_called()
