"""Bevestigen kan alleen na een /start die de controle heeft doorstaan (RC-329).

Het versleutelde zaad reist in een verborgen formulierveld en niet in de sessiecookie,
want die draagt al een id_token en loopt met een zaad erbij tegen de 4 KB van een browser
aan. De sessie draagt daarom alleen de VINGERAFDRUK van het zaad. Zonder die controle zou
wie een sessie overneemt een eigen zaad kunnen aanleveren (de publieke sleutel is
publiek) en daarmee de bestaande factor vervangen zonder de huidige code te kennen.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from opi.web.router_otp import PENDING_FIELD, PENDING_SESSION_KEY, _fingerprint, otp_afbreken, otp_bevestigen

EMAIL = "iemand@rijksoverheid.nl"
ZAAD = "-----BEGIN AGE ENCRYPTED FILE-----versleuteld-----END AGE ENCRYPTED FILE-----"


def _verzoek(session: dict[str, Any], velden: dict[str, str]) -> Any:
    verzoek = MagicMock()
    verzoek.state = SimpleNamespace(user={"email": EMAIL})
    verzoek.session = session
    verzoek.form = AsyncMock(return_value=velden)
    return verzoek


def _dienst() -> Any:
    dienst = MagicMock()
    dienst.confirm_registration = AsyncMock(return_value=True)
    return dienst


async def test_zonder_vingerafdruk_in_de_sessie_wordt_er_niets_bevestigd() -> None:
    dienst = _dienst()
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek({}, {PENDING_FIELD: ZAAD, "code": "123456"}))

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp"
    dienst.confirm_registration.assert_not_awaited()


async def test_een_ander_zaad_dan_dat_van_start_wordt_niet_bevestigd() -> None:
    dienst = _dienst()
    sessie = {PENDING_SESSION_KEY: _fingerprint(ZAAD)}
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek(sessie, {PENDING_FIELD: "een eigen zaad", "code": "123456"}))

    assert antwoord.status_code == 303
    dienst.confirm_registration.assert_not_awaited()


async def test_het_zaad_van_start_wordt_wel_bevestigd() -> None:
    dienst = _dienst()
    sessie = {PENDING_SESSION_KEY: _fingerprint(ZAAD)}
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek(sessie, {PENDING_FIELD: ZAAD, "code": "123456"}))

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp?melding=Tweede+factor+ingericht"
    dienst.confirm_registration.assert_awaited_once()
    # En de vingerafdruk is verbruikt, dus een tweede keer bevestigen lukt niet.
    assert PENDING_SESSION_KEY not in sessie


async def test_afbreken_vergeet_de_vingerafdruk_en_laat_de_bestaande_factor_staan() -> None:
    sessie = {PENDING_SESSION_KEY: _fingerprint(ZAAD)}
    antwoord = await otp_afbreken(_verzoek(sessie, {}))

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp?melding=Registratie+afgebroken"
    assert PENDING_SESSION_KEY not in sessie
