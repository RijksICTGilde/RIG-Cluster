"""Vervangen van een bestaande, bevestigde factor vraagt eerst een geldige code (RC-329).

``otp_bevestigen`` wordt al getoetst in ``test_otp_bevestigen_zonder_start.py``; dit
bestand toetst de andere helft van hetzelfde besluit (8.2): /start zelf, voor wie al een
bevestigde factor heeft. Zonder deze controle neemt wie een sessie overneemt ook de
factor over.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from opi.services.user_otp_service import OtpRegistration, OtpStatus, OtpVerification
from opi.web.router_otp import otp_start

EMAIL = "iemand@rijksoverheid.nl"

_REGISTRATIE = OtpRegistration(
    secret="ZZZ",
    encrypted_secret="-----BEGIN AGE ENCRYPTED FILE-----nieuw-----END AGE ENCRYPTED FILE-----",
    otpauth_uri="otpauth://totp/test",
    base32_secret="ZZZ",
)


def _verzoek(velden: dict[str, str]) -> Any:
    verzoek = MagicMock()
    verzoek.state = SimpleNamespace(user={"email": EMAIL})
    verzoek.session = {}
    verzoek.form = AsyncMock(return_value=velden)
    return verzoek


def _dienst(status: OtpStatus, verify_uitkomst: OtpVerification) -> Any:
    dienst = MagicMock()
    dienst.status = AsyncMock(return_value=status)
    dienst.verify = AsyncMock(return_value=verify_uitkomst)
    dienst.start_registration = AsyncMock(return_value=_REGISTRATIE)
    # Sinds PR 370 legt /start de lopende registratie server-side vast; zonder AsyncMock
    # hier eindigde deze toets op "'MagicMock' object can't be awaited".
    dienst.note_pending_registration = AsyncMock()
    return dienst


async def test_zonder_bestaande_factor_start_meteen_een_nieuwe_registratie() -> None:
    dienst = _dienst(OtpStatus(registered=False), OtpVerification.NOT_REGISTERED)
    with (
        patch("opi.web.router_otp._get_otp_service", return_value=dienst),
        patch("opi.web.router_otp._registration_page", return_value="de registratiepagina") as pagina,
    ):
        antwoord = await otp_start(_verzoek({}))

    assert antwoord == "de registratiepagina"
    dienst.start_registration.assert_awaited_once_with(EMAIL)
    pagina.assert_called_once()


async def test_een_bestaande_factor_vervangen_met_de_juiste_code_lukt() -> None:
    dienst = _dienst(OtpStatus(registered=True), OtpVerification.OK)
    with (
        patch("opi.web.router_otp._get_otp_service", return_value=dienst),
        patch("opi.web.router_otp._registration_page", return_value="de registratiepagina"),
    ):
        antwoord = await otp_start(_verzoek({"huidige_code": "123456"}))

    assert antwoord == "de registratiepagina"
    dienst.start_registration.assert_awaited_once_with(EMAIL)


async def test_een_bestaande_factor_vervangen_zonder_de_juiste_code_start_niets() -> None:
    dienst = _dienst(OtpStatus(registered=True), OtpVerification.INVALID)
    with (
        patch("opi.web.router_otp._get_otp_service", return_value=dienst),
        patch("opi.web.router_otp._status_page", return_value="de statuspagina") as pagina,
    ):
        antwoord = await otp_start(_verzoek({"huidige_code": "000000"}))

    assert antwoord == "de statuspagina"
    dienst.start_registration.assert_not_awaited()
    context = pagina.call_args[0][1]
    assert "klopt niet" in context["fout"]


async def test_een_bestaande_factor_vervangen_tijdens_een_blokkade_meldt_dat() -> None:
    dienst = _dienst(OtpStatus(registered=True), OtpVerification.LOCKED)
    with (
        patch("opi.web.router_otp._get_otp_service", return_value=dienst),
        patch("opi.web.router_otp._status_page", return_value="de statuspagina") as pagina,
    ):
        antwoord = await otp_start(_verzoek({"huidige_code": "000000"}))

    assert antwoord == "de statuspagina"
    dienst.start_registration.assert_not_awaited()
    context = pagina.call_args[0][1]
    assert "geblokkeerd" in context["fout"]
