"""Uitloggen maakt eerdere sessies van dezelfde gebruiker ongeldig (RC-287).

Zonder register van sessies bleef een cookie die elders was uitgegeven geldig tot hij
verliep: uitloggen wiste alleen de cookie in die ene browser. Het register is de tabel
``session_revocations``, en de ``UserService`` houdt hem in het geheugen bij.

Deze toetsen draaien tegen een echte Postgres (``orm_db``), zodat het intrekken via de
tabel loopt en niet via een stub.
"""

from __future__ import annotations

import secrets
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import opi.services.persistence  # noqa: F401  -- registreert de ORM-modellen op Base.metadata
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from opi.api.auth_routes import logout
from opi.core.auth_decorators import requires_sso
from opi.core.config import settings
from opi.core.db import Base, include_orm_object, session_scope
from opi.core.session_guard import valid_session_user
from opi.middleware.authorization import AuthorizationMiddleware
from opi.middleware.server_side_session import ServerSideSessionMiddleware
from opi.services.persistence.sessions import PortalSession
from opi.services.session_revocation_service import load_revocations, revoke_sessions
from opi.services.session_store import get_session_store
from opi.services.user_admin_service import UserAdminService
from opi.services.user_service import get_user_service
from opi.web.router_user_admin import revoke_user_sessions
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from tests.conftest import alembic_downgrade, alembic_upgrade
from tests.inmemory_session_store import InMemorySessionStore

if TYPE_CHECKING:
    from collections.abc import Iterator

EMAIL = "intrekken@rijksoverheid.nl"
BEHEERDER = "beheerder@rijksoverheid.nl"
EIND_SESSIE = "https://keycloak.test/logout"


def _cookie(store: InMemorySessionStore, login_at: float, email: str = EMAIL) -> str:
    """Een sessie in de store; de cookie draagt alleen het ondertekende id (RC-390)."""
    session_id = store.seed(secrets.token_urlsafe(32), _sessie(login_at, email))
    return TimestampSigner(str(settings.SECRET_KEY)).sign(session_id.encode()).decode()


def _sessie(login_at: float, email: str = EMAIL) -> dict[str, Any]:
    return {"user": {"email": email, "login_at": login_at}}


@pytest.fixture
def client() -> Iterator[tuple[TestClient, InMemorySessionStore]]:
    """Een app met de autorisatie en de server-side sessie erin, met een in-memory store.

    De guard-semantiek (ingetrokken = geweigerd) toetst op de echte UserService; de
    sessie-opslag is hier in-memory, want de TestClient draait zijn eigen event loop.
    """
    store = InMemorySessionStore()
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

    toegang = MagicMock()
    toegang.is_email_allowed.return_value = True
    with patch("opi.middleware.authorization.get_user_service", return_value=toegang):
        yield TestClient(app), store


@pytest.fixture(autouse=True)
def _geen_intrekking_laten_staan() -> Iterator[None]:
    """De UserService is een singleton die de hele run meegaat.

    Zonder dit zou een intrekking uit deze module de sessies van dezelfde e-mailadressen
    in een volgende toets ook weigeren.
    """
    yield
    for email in (EMAIL, BEHEERDER):
        get_user_service().set_sessions_revoked_after(email, 0.0)


def _uitlogverzoek(session: dict[str, Any]) -> Any:
    """Zonder Keycloak-client op de oauth-state eindigt het uitloggen op "/" in plaats van
    op het end-session-endpoint; dat deel is hier niet aan de beurt."""
    return SimpleNamespace(session=session, app=SimpleNamespace(state=SimpleNamespace(oauth=SimpleNamespace())))


async def test_uitloggen_maakt_een_sessie_op_een_ander_apparaat_ongeldig(orm_db: None) -> None:
    elders = _sessie(time.time() - 120)
    assert valid_session_user(elders) is not None

    antwoord = await logout(_uitlogverzoek(_sessie(time.time() - 60)))

    assert antwoord.status_code == 302
    assert valid_session_user(elders) is None


async def test_een_falende_database_breekt_het_uitloggen_niet_af() -> None:
    """Een onbereikbare rig-db mag de end-session van Keycloak niet overslaan: zonder die
    stap blijft de SSO-sessie staan en logt het volgende bezoek stil opnieuw in."""
    keycloak = SimpleNamespace(load_server_metadata=AsyncMock(return_value={"end_session_endpoint": EIND_SESSIE}))
    verzoek = SimpleNamespace(
        session=_sessie(time.time() - 60),
        base_url="https://zad.test/",
        app=SimpleNamespace(state=SimpleNamespace(oauth=SimpleNamespace(keycloak=keycloak))),
    )

    with patch("opi.api.auth_routes.revoke_sessions", side_effect=SQLAlchemyError("database weg")):
        antwoord = await logout(verzoek)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"].startswith(EIND_SESSIE)


async def test_de_middleware_weigert_een_ingetrokken_sessie_en_laat_een_nieuwe_door(
    orm_db: None, client: tuple[TestClient, InMemorySessionStore]
) -> None:
    http, store = client
    await revoke_sessions(EMAIL)

    http.cookies.set("session", _cookie(store, time.time() - 60))
    geweigerd = http.get("/beveiligd", follow_redirects=False)

    http.cookies.set("session", _cookie(store, time.time()))
    opnieuw = http.get("/beveiligd", follow_redirects=False)

    assert geweigerd.status_code == 302
    assert geweigerd.headers["location"] == "/auth/login"
    assert opnieuw.status_code == 200


async def test_revocation_removes_the_server_side_session_rows(orm_db: None) -> None:
    """Sinds RC-390 is "verwijder de rij" de harde weigering op de volgende request:
    naast de revocatiepost verdwijnen de sessies van dit adres uit de tabel."""
    await get_session_store().set(secrets.token_urlsafe(32), _sessie(time.time() - 60, EMAIL))
    await get_session_store().set(
        secrets.token_urlsafe(32), _sessie(time.time() - 60, "iemand-anders@rijksoverheid.nl")
    )

    await revoke_sessions(EMAIL)

    async with session_scope() as session:
        over = (await session.scalars(select(PortalSession))).all()
    assert [rij.data["user"]["email"] for rij in over] == ["iemand-anders@rijksoverheid.nl"]


async def test_het_intrekken_staat_in_de_tabel_en_overleeft_een_herstart(orm_db: None) -> None:
    """Het geheugen wordt bij het opstarten uit de tabel gevuld, dus een pod die herstart
    weigert dezelfde sessies nog."""
    ingetrokken_vanaf = await revoke_sessions(EMAIL)

    get_user_service().set_sessions_revoked_after(EMAIL, 0.0)
    assert await load_revocations() == 1

    assert get_user_service().are_sessions_revoked(EMAIL, ingetrokken_vanaf.timestamp() - 1)
    assert not get_user_service().are_sessions_revoked(EMAIL, ingetrokken_vanaf.timestamp() + 1)


# ---------------------------------------------------------------------------
# De platformbeheerder
# ---------------------------------------------------------------------------


def _verzoek(email: str | None) -> Any:
    user = {"email": email} if email else None
    return SimpleNamespace(state=SimpleNamespace(user=user), query_params={})


def _gebruikersdienst(admins: set[str]) -> Any:
    dienst = MagicMock()
    dienst.is_platform_admin.side_effect = lambda email: email in admins
    return dienst


async def test_een_platformbeheerder_beeindigt_de_sessies_van_een_gebruiker(orm_db: None) -> None:
    aangemaakt = await UserAdminService().create_user(email=EMAIL, full_name="Iemand")
    sessie = _sessie(time.time() - 60)

    with patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst({BEHEERDER})):
        antwoord = await revoke_user_sessions(_verzoek(BEHEERDER), aangemaakt["id"])

    assert antwoord.status_code == 303
    assert valid_session_user(sessie) is None


async def test_een_gewone_gebruiker_mag_de_sessies_van_een_ander_niet_beeindigen(orm_db: None) -> None:
    aangemaakt = await UserAdminService().create_user(email=EMAIL, full_name="Iemand")

    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst(set())),
        pytest.raises(HTTPException) as fout,
    ):
        await revoke_user_sessions(_verzoek("ontwikkelaar@rijksoverheid.nl"), aangemaakt["id"])

    assert fout.value.status_code == 403
    assert valid_session_user(_sessie(time.time() - 60)) is not None


# ---------------------------------------------------------------------------
# De migratie
# ---------------------------------------------------------------------------


def test_migratie_008_maakt_de_tabel_zoals_het_model_hem_beschrijft(migration_db: str) -> None:
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "head")
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"include_object": include_orm_object, "target_metadata": Base.metadata}
            )
            afwijking = [d for d in compare_metadata(context, Base.metadata) if "session_revocations" in repr(d)]
            conn.execute(text("INSERT INTO session_revocations (email, revoked_after) VALUES ('a@b.nl', now())"))
            conn.commit()
            rijen = conn.execute(text("SELECT email FROM session_revocations")).all()
    finally:
        engine.dispose()

    assert afwijking == []
    assert rijen == [("a@b.nl",)]


def test_de_migratie_is_terug_te_draaien(migration_db: str) -> None:
    """``upgrade head`` en daarna terug naar 007 laten een lege database heel.

    Terug naar 007 en niet ``-1``: komt er een migratie bovenop 008, dan draait ``-1``
    die terug in plaats van deze.
    """
    engine = create_engine(migration_db)
    try:
        alembic_upgrade(engine, "head")
        alembic_downgrade(engine, "007")
        with engine.connect() as conn:
            bestaat = conn.execute(text("SELECT to_regclass('session_revocations')")).scalar()
    finally:
        engine.dispose()

    assert bestaat is None
