"""Bevestigen kan alleen na een /start die de controle heeft doorstaan (RC-329, vernieuwd 27-10).

De lopende registratie staat server-side in de tabel ``otp_pending_registrations``
(vingerafdruk van het zaad, plus verloop). Tot 27-10 zat die in de sessiecookie, op een
sessie met een volle id_token liep die erover heen en de browser liet hem geruisloos
vallen: bevestigen deed toen niets, zonder melding. Dezelfde oplossing als AMT/TAD.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from opi.web.router_otp import PENDING_FIELD, _fingerprint, otp_afbreken, otp_bevestigen, otp_start

EMAIL = "iemand@rijksoverheid.nl"
ZAAD = "-----BEGIN AGE ENCRYPTED FILE-----versleuteld-----END AGE ENCRYPTED FILE-----"


def _verzoek(velden: dict[str, str]) -> Any:
    verzoek = MagicMock()
    verzoek.state = SimpleNamespace(user={"email": EMAIL})
    verzoek.form = AsyncMock(return_value=velden)
    return verzoek


def _dienst(**overrides: Any) -> Any:
    dienst = MagicMock()
    dienst.confirm_registration = AsyncMock(return_value=True)
    dienst.consume_pending_registration = AsyncMock(return_value=True)
    dienst.note_pending_registration = AsyncMock()
    dienst.discard_pending_registration = AsyncMock()
    for naam, waarde in overrides.items():
        setattr(dienst, naam, waarde)
    return dienst


def _melding(antwoord: Any) -> str:
    query = parse_qs(urlparse(antwoord.headers["location"]).query)
    return query["melding"][0]


async def test_zonder_lopende_registratie_wordt_er_niets_bevestigd() -> None:
    dienst = _dienst(consume_pending_registration=AsyncMock(return_value=False))
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek({PENDING_FIELD: ZAAD, "code": "123456"}))

    assert antwoord.status_code == 303
    assert "Begin opnieuw" in _melding(antwoord).replace("+", " ")
    dienst.confirm_registration.assert_not_awaited()


async def test_bij_een_ander_zaad_dan_dat_van_start_wordt_er_niets_bevestigd() -> None:
    """De rij hoort bij het zaad uit /start; een eigen zaad heeft een andere afdruk."""
    dienst = _dienst(consume_pending_registration=AsyncMock(return_value=False))
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek({PENDING_FIELD: "een eigen zaad", "code": "123456"}))

    assert antwoord.status_code == 303
    dienst.confirm_registration.assert_not_awaited()
    dienst.consume_pending_registration.assert_awaited_once()


async def test_het_zaad_van_start_wordt_wel_bevestigd() -> None:
    dienst = _dienst()
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek({PENDING_FIELD: ZAAD, "code": "123456"}))

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp?melding=Tweede+factor+ingericht"
    dienst.confirm_registration.assert_awaited_once()
    # De controle liep over de vingerafdruk van precies dit zaad.
    dienst.consume_pending_registration.assert_awaited_once_with(EMAIL, _fingerprint(ZAAD))


async def test_een_formulier_post_met_crlf_regels_bevestigt_alsnog() -> None:
    """HTML maakt CRLF van de LF's in het AGE-pantser tussen renderen en posten.

    De echte (niet de in-memory e2e-stub) versleuteling is meerregelig; zonder de
    normalisatie in ``otp_bevestigen`` week de geposte afdruk af van die uit /start en
    faalde elke bevestiging (op de sandbox gemeten 27-10-2026).
    """
    pantser = "-----BEGIN AGE ENCRYPTED FILE-----\nregel-1\nregel-2\n-----END AGE ENCRYPTED FILE-----\n"
    dienst = _dienst()
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek({PENDING_FIELD: pantser.replace("\n", "\r\n"), "code": "123456"}))

    assert "Begin opnieuw" not in _melding(antwoord).replace("+", " ")
    dienst.consume_pending_registration.assert_awaited_once_with(EMAIL, _fingerprint(pantser))
    dienst.confirm_registration.assert_awaited_once()


async def test_afbreken_vergeet_de_rij_en_laat_de_bestaande_factor_staan() -> None:
    dienst = _dienst()
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_afbreken(_verzoek({}))

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp?melding=Registratie+afgebroken"
    dienst.discard_pending_registration.assert_awaited_once_with(EMAIL)


async def test_start_noteert_de_registratie_bij_de_service() -> None:
    dienst = _dienst()
    registratie = SimpleNamespace(encrypted_secret=ZAAD, otpauth_uri="otpauth://x", base32_secret="JBSWY3DPEHPK3PXP")
    dienst.start_registration = AsyncMock(return_value=registratie)
    dienst.status = AsyncMock(return_value=SimpleNamespace(registered=False))

    verzoek = MagicMock()
    verzoek.state = SimpleNamespace(user={"email": EMAIL})
    with (
        patch("opi.web.router_otp._get_otp_service", return_value=dienst),
        patch("opi.web.router_otp._registration_page", return_value=MagicMock(status_code=200)) as pagina,
        patch("opi.web.router_otp.get_current_user", return_value={"email": EMAIL}),
    ):
        await otp_start(verzoek)

    dienst.note_pending_registration.assert_awaited_once_with(EMAIL, _fingerprint(ZAAD))
    assert pagina.called
