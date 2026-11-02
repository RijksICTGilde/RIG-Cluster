"""Een 2-factor inrichten of vervangen vraagt eerst een herlogin bij Keycloak (28-10).

Zonder die poort richt iedereen met een open sessie op een ontgrendelde computer een eigen
2-factor in onder de naam van een ander. De state van de herlogin staat server-side
(``otp_herlogins``), niet in de sessiecookie.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from joserfc.errors import InvalidClaimError
from opi.services import otp_herlogin_service
from opi.services.otp_herlogin_service import Herlogin, OtpHerloginService, herlogin_klopt
from opi.web.router_otp import otp_herlogin, otp_herlogin_terug, otp_start

EMAIL = "iemand@rijksoverheid.nl"
LOGIN_AT = 1_700_000_000.0
NU = 1_700_000_500.0
USER = {"email": EMAIL, "sub": "sub-1", "login_at": LOGIN_AT}
HERLOGIN = Herlogin(state="st", nonce="no", code_verifier="cv")


def _verzoek(query: dict[str, str] | None = None) -> Any:
    verzoek = MagicMock()
    verzoek.state = SimpleNamespace(user=USER)
    verzoek.query_params = query or {}
    verzoek.url_for = MagicMock(return_value="https://opi.test/account/otp/herlogin/terug")
    verzoek.app.state.oauth.keycloak.create_authorization_url = AsyncMock(
        return_value={"url": "https://keycloak.test/auth?x=1"}
    )
    return verzoek


def _dienst(**overrides: Any) -> Any:
    dienst = MagicMock()
    dienst.is_fresh = AsyncMock(return_value=False)
    dienst.begin = AsyncMock(return_value=HERLOGIN)
    dienst.take = AsyncMock(return_value=HERLOGIN)
    dienst.mark_verified = AsyncMock()
    for naam, waarde in overrides.items():
        setattr(dienst, naam, waarde)
    return dienst


def _claims(**overrides: Any) -> dict[str, Any]:
    claims = {"sub": "sub-1", "email": EMAIL, "email_verified": True, "auth_time": NU - 5}
    claims.update(overrides)
    return claims


# --- de poort op /start -------------------------------------------------------------------


async def test_start_zonder_verse_herlogin_stuurt_naar_de_herlogin() -> None:
    otp = MagicMock()
    verzoek = _verzoek()
    verzoek.form = AsyncMock(return_value={})
    with (
        patch("opi.web.router_otp._get_herlogin_service", return_value=_dienst()),
        patch("opi.web.router_otp._get_otp_service", return_value=otp),
    ):
        antwoord = await otp_start(verzoek)

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp/herlogin"
    # Er is nog geen seed gemaakt en geen registratie genoteerd.
    otp.start_registration.assert_not_called()


# --- de herlogin zelf ---------------------------------------------------------------------


async def test_herlogin_dwingt_een_verse_login_af_met_server_side_state() -> None:
    verzoek = _verzoek()
    dienst = _dienst()
    with patch("opi.web.router_otp._get_herlogin_service", return_value=dienst):
        antwoord = await otp_herlogin(verzoek)

    assert antwoord.status_code == 302
    assert antwoord.headers["location"] == "https://keycloak.test/auth?x=1"
    dienst.begin.assert_awaited_once_with(EMAIL, LOGIN_AT)
    aanroep = verzoek.app.state.oauth.keycloak.create_authorization_url.await_args
    assert aanroep.kwargs["prompt"] == "login"
    assert aanroep.kwargs["max_age"] == 0
    assert (aanroep.kwargs["state"], aanroep.kwargs["nonce"], aanroep.kwargs["code_verifier"]) == ("st", "no", "cv")


async def test_terug_met_een_geldige_verse_login_opent_het_venster() -> None:
    verzoek = _verzoek({"state": "st", "code": "abc"})
    keycloak = verzoek.app.state.oauth.keycloak
    keycloak.fetch_access_token = AsyncMock(return_value={"id_token": "x"})
    keycloak.parse_id_token = AsyncMock(return_value=_claims(auth_time=datetime.now(UTC).timestamp()))
    dienst = _dienst()
    with patch("opi.web.router_otp._get_herlogin_service", return_value=dienst):
        antwoord = await otp_herlogin_terug(verzoek)

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp"
    dienst.mark_verified.assert_awaited_once_with("st")
    keycloak.fetch_access_token.assert_awaited_once()
    assert keycloak.fetch_access_token.await_args.kwargs == {"code": "abc", "code_verifier": "cv"}


@pytest.mark.parametrize(
    "query",
    [{"state": "onbekend", "code": "abc"}, {"state": "st"}, {"state": "st", "error": "access_denied"}],
    ids=["onbekende-state", "geen-code", "keycloak-weigert"],
)
async def test_terug_zonder_geldige_state_of_code_opent_niets(query: dict[str, str]) -> None:
    verzoek = _verzoek(query)
    dienst = _dienst(take=AsyncMock(return_value=None if query["state"] == "onbekend" else HERLOGIN))
    otp = MagicMock()
    otp.status = AsyncMock(return_value=SimpleNamespace(registered=False))
    with (
        patch("opi.web.router_otp._get_herlogin_service", return_value=dienst),
        patch("opi.web.router_otp._get_otp_service", return_value=otp),
        patch("opi.web.router_otp._status_page", return_value=MagicMock(status_code=200)) as pagina,
    ):
        antwoord = await otp_herlogin_terug(verzoek)

    assert antwoord.status_code == 200
    assert "niet gelukt" in pagina.call_args.args[1]["fout"]
    dienst.mark_verified.assert_not_awaited()


async def test_terug_met_een_nonce_die_niet_klopt_opent_niets() -> None:
    """De nonce toetst authlib in ``parse_id_token``; die fout mag geen venster openen.

    ``authlib.oidc.core.claims`` werpt de ``joserfc``-variant van ``InvalidClaimError``, niet
    de ``authlib.jose``-variant. Dat geldt ook voor ``iss`` en ``aud``.
    """
    verzoek = _verzoek({"state": "st", "code": "abc"})
    keycloak = verzoek.app.state.oauth.keycloak
    keycloak.fetch_access_token = AsyncMock(return_value={"id_token": "x"})
    keycloak.parse_id_token = AsyncMock(side_effect=InvalidClaimError("nonce"))
    dienst = _dienst()
    otp = MagicMock()
    otp.status = AsyncMock(return_value=SimpleNamespace(registered=False))
    with (
        patch("opi.web.router_otp._get_herlogin_service", return_value=dienst),
        patch("opi.web.router_otp._get_otp_service", return_value=otp),
        patch("opi.web.router_otp._status_page", return_value=MagicMock(status_code=200)) as pagina,
    ):
        antwoord = await otp_herlogin_terug(verzoek)

    assert antwoord.status_code == 200
    assert "niet gelukt" in pagina.call_args.args[1]["fout"]
    dienst.mark_verified.assert_not_awaited()


async def test_start_toetst_de_herlogin_van_deze_sessie() -> None:
    """Het venster hangt aan ``login_at``, dus een herlogin uit een andere sessie telt niet."""
    otp = MagicMock()
    verzoek = _verzoek()
    verzoek.form = AsyncMock(return_value={})
    dienst = _dienst(is_fresh=AsyncMock(return_value=False))
    with (
        patch("opi.web.router_otp._get_herlogin_service", return_value=dienst),
        patch("opi.web.router_otp._get_otp_service", return_value=otp),
    ):
        await otp_start(verzoek)

    dienst.is_fresh.assert_awaited_once_with(EMAIL, LOGIN_AT)


async def test_terug_met_een_fout_van_keycloak_opent_niets() -> None:
    verzoek = _verzoek({"state": "st", "code": "abc"})
    verzoek.app.state.oauth.keycloak.fetch_access_token = AsyncMock(side_effect=httpx.ConnectError("weg"))
    dienst = _dienst()
    otp = MagicMock()
    otp.status = AsyncMock(return_value=SimpleNamespace(registered=False))
    with (
        patch("opi.web.router_otp._get_herlogin_service", return_value=dienst),
        patch("opi.web.router_otp._get_otp_service", return_value=otp),
        patch("opi.web.router_otp._status_page", return_value=MagicMock(status_code=200)),
    ):
        antwoord = await otp_herlogin_terug(verzoek)

    assert antwoord.status_code == 200
    dienst.mark_verified.assert_not_awaited()


# --- welke claims tellen ------------------------------------------------------------------


def test_verse_claims_van_de_ingelogde_gebruiker_kloppen() -> None:
    assert herlogin_klopt(_claims(), USER, now=NU) is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": "een-ander"},
        {"email": "ander@rijksoverheid.nl"},
        {"email_verified": False},
        {"auth_time": NU - 3600},
        {"auth_time": NU + 3600},
        {"auth_time": None},
    ],
    ids=[
        "andere-sub",
        "ander-adres",
        "ongeverifieerd",
        "oude-auth-time",
        "auth-time-uit-de-toekomst",
        "geen-auth-time",
    ],
)
def test_claims_van_een_ander_of_oude_login_kloppen_niet(overrides: dict[str, Any]) -> None:
    assert herlogin_klopt(_claims(**overrides), USER, now=NU) is False


# --- de tabel ------------------------------------------------------------------------------


async def test_een_geslaagde_herlogin_geldt_alleen_voor_die_sessie(orm_db) -> None:
    dienst = OtpHerloginService()
    herlogin = await dienst.begin(EMAIL, LOGIN_AT)
    assert await dienst.is_fresh(EMAIL, LOGIN_AT) is False, "begonnen is niet geslaagd"

    assert await dienst.take(herlogin.state, EMAIL, LOGIN_AT + 1) is None, "andere sessie"
    assert await dienst.take(herlogin.state, "ander@rijksoverheid.nl", LOGIN_AT) is None, "andere gebruiker"
    assert await dienst.take("onbekend", EMAIL, LOGIN_AT) is None
    assert await dienst.take(herlogin.state, EMAIL, LOGIN_AT) == herlogin

    await dienst.mark_verified(herlogin.state)
    assert await dienst.is_fresh(EMAIL, LOGIN_AT) is True
    assert await dienst.is_fresh(EMAIL, LOGIN_AT + 1) is False, "een andere sessie van dezelfde gebruiker"
    assert await dienst.is_fresh("ander@rijksoverheid.nl", LOGIN_AT) is False
    assert await dienst.is_fresh(EMAIL, None) is False
    assert await dienst.take(herlogin.state, EMAIL, LOGIN_AT) is None, "een state geldt maar een keer"


async def test_het_venster_verloopt_en_schuift_niet_mee(orm_db, monkeypatch: pytest.MonkeyPatch) -> None:
    dienst = OtpHerloginService()
    with monkeypatch.context() as kort:
        kort.setattr(otp_herlogin_service, "HERLOGIN_VENSTER", timedelta(seconds=-1))
        herlogin = await dienst.begin(EMAIL, LOGIN_AT)
        await dienst.mark_verified(herlogin.state)

    assert await dienst.is_fresh(EMAIL, LOGIN_AT) is False
    assert await dienst.is_fresh(EMAIL, LOGIN_AT) is False, "een toets verlengt het venster niet"
    assert await dienst.purge_expired() == 1
    assert await dienst.purge_expired() == 0


async def test_een_verlopen_poging_om_in_te_loggen_telt_niet(orm_db, monkeypatch: pytest.MonkeyPatch) -> None:
    dienst = OtpHerloginService()
    monkeypatch.setattr(otp_herlogin_service, "HERLOGIN_TTL", timedelta(seconds=-1))
    herlogin = await dienst.begin(EMAIL, LOGIN_AT)

    assert await dienst.take(herlogin.state, EMAIL, LOGIN_AT) is None
