"""Bevestigen kan alleen na een /start die de controle heeft doorstaan (RC-329, vernieuwd 27-10).

De lopende registratie staat server-side in de tabel ``otp_pending_registrations``
(vingerafdruk van de seed, plus verloop). Tot 27-10 zat die in de sessiecookie, op een
sessie met een volle id_token liep die erover heen en de browser liet hem geruisloos
vallen: bevestigen deed toen niets, zonder melding. Dezelfde oplossing als AMT/TAD.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.web.router_otp import PENDING_FIELD, _fingerprint, otp_afbreken, otp_bevestigen, otp_start

if TYPE_CHECKING:
    from collections.abc import Iterator

EMAIL = "iemand@rijksoverheid.nl"


@pytest.fixture(autouse=True)
def _herlogin_is_vers() -> Iterator[None]:
    """Deze tests gaan over wat NA de herlogin komt; de poort zelf staat in test_otp_herlogin.py."""
    dienst = MagicMock()
    dienst.is_fresh = AsyncMock(return_value=True)
    with patch("opi.web.router_otp._get_herlogin_service", return_value=dienst):
        yield


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
    dienst.renew_expired_pending_registration = AsyncMock(return_value=False)
    dienst.discard_pending_registration = AsyncMock()
    for naam, waarde in overrides.items():
        setattr(dienst, naam, waarde)
    return dienst


async def test_zonder_lopende_registratie_wordt_er_niets_bevestigd() -> None:
    dienst = _dienst(consume_pending_registration=AsyncMock(return_value=False))
    dienst.status = AsyncMock(return_value=SimpleNamespace(registered=False))
    with (
        patch("opi.web.router_otp._get_otp_service", return_value=dienst),
        patch("opi.web.router_otp._status_page", return_value=MagicMock(status_code=200)) as pagina,
    ):
        antwoord = await otp_bevestigen(_verzoek({PENDING_FIELD: ZAAD, "code": "123456"}))

    # Geen redirect met de melding in de URL: de fout staat in het antwoord zelf.
    assert antwoord.status_code == 200
    assert "Begin opnieuw" in pagina.call_args.args[1]["fout"]
    dienst.confirm_registration.assert_not_awaited()


async def test_bij_een_andere_seed_dan_die_van_start_wordt_er_niets_bevestigd() -> None:
    """De rij hoort bij de seed uit /start; een eigen seed heeft een andere afdruk."""
    dienst = _dienst(consume_pending_registration=AsyncMock(return_value=False))
    dienst.status = AsyncMock(return_value=SimpleNamespace(registered=False))
    with (
        patch("opi.web.router_otp._get_otp_service", return_value=dienst),
        patch("opi.web.router_otp._status_page", return_value=MagicMock(status_code=200)),
    ):
        antwoord = await otp_bevestigen(_verzoek({PENDING_FIELD: "een eigen seed", "code": "123456"}))

    assert antwoord.status_code == 200
    dienst.confirm_registration.assert_not_awaited()
    dienst.consume_pending_registration.assert_awaited_once()


async def test_de_seed_van_start_wordt_wel_bevestigd() -> None:
    dienst = _dienst()
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_bevestigen(_verzoek({PENDING_FIELD: ZAAD, "code": "123456"}))

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp"
    dienst.confirm_registration.assert_awaited_once()
    # De controle liep over de vingerafdruk van precies deze seed.
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

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp"
    dienst.consume_pending_registration.assert_awaited_once_with(EMAIL, _fingerprint(pantser))
    dienst.confirm_registration.assert_awaited_once()


async def test_afbreken_vergeet_de_rij_en_laat_de_bestaande_2_factor_staan() -> None:
    dienst = _dienst()
    with patch("opi.web.router_otp._get_otp_service", return_value=dienst):
        antwoord = await otp_afbreken(_verzoek({}))

    assert antwoord.status_code == 303
    assert antwoord.headers["location"] == "/account/otp"
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
