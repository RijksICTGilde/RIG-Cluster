"""Wie de tweede factor van een ander mag leegmaken (RC-329).

Alleen een platformbeheerder, en niet voor zichzelf: je eigen factor zonder tweede factor
kunnen wissen maakt van elke overgenomen beheerderssessie een weg om die factor heen.
Vervangen kan wel, op /account/otp, en dat vraagt een code van de bestaande factor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.step_up import STEP_UP_SESSION_KEY
from opi.web.router_user_admin import reset_user_otp

BEHEERDER = "beheerder@rijksoverheid.nl"
ANDER = "ander@rijksoverheid.nl"
USER_ID = "11111111-1111-1111-1111-111111111111"


def _verzoek(email: str | None) -> Any:
    """Een verzoek MET verse step-up: die vraagt de route sinds RC-409, en deze toetsen
    gaan over wie mag, niet over de tweede factor (``test_beheerroutes_portaal_step_up``)."""
    user = {"email": email} if email else None
    return SimpleNamespace(
        state=SimpleNamespace(user=user),
        query_params={},
        session={STEP_UP_SESSION_KEY: datetime.now(UTC).isoformat()},
        method="POST",
        url=SimpleNamespace(path=f"/admin/users/{USER_ID}/otp-reset"),
        headers={},
    )


def _gebruikersdienst(admins: set[str]) -> Any:
    dienst = MagicMock()
    dienst.is_platform_admin.side_effect = lambda email: email in admins
    return dienst


def _gebruikersbeheer(email: str | None) -> Any:
    dienst = MagicMock()
    dienst.get_user = AsyncMock(return_value={"id": USER_ID, "email": email} if email else None)
    return dienst


def _otp_dienst() -> Any:
    dienst = MagicMock()
    dienst.reset = AsyncMock(return_value=True)
    return dienst


async def test_een_niet_beheerder_komt_er_niet_in() -> None:
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst(set())),
        pytest.raises(HTTPException) as fout,
    ):
        await reset_user_otp(_verzoek(ANDER), USER_ID)

    assert fout.value.status_code == 403


async def test_zonder_sessie_komt_er_niemand_in() -> None:
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst({BEHEERDER})),
        pytest.raises(HTTPException) as fout,
    ):
        await reset_user_otp(_verzoek(None), USER_ID)

    assert fout.value.status_code == 401


async def test_een_beheerder_maakt_de_factor_van_een_ander_leeg() -> None:
    otp = _otp_dienst()
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst({BEHEERDER})),
        patch("opi.web.router_user_admin._get_service", return_value=_gebruikersbeheer(ANDER)),
        patch("opi.web.router_user_admin._get_otp_service", return_value=otp),
    ):
        antwoord = await reset_user_otp(_verzoek(BEHEERDER), USER_ID)

    assert antwoord.status_code == 303
    otp.reset.assert_awaited_once_with(ANDER, by=BEHEERDER)


async def test_een_beheerder_reset_zijn_eigen_factor_niet_zo() -> None:
    otp = _otp_dienst()
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst({BEHEERDER})),
        patch("opi.web.router_user_admin._get_service", return_value=_gebruikersbeheer(BEHEERDER)),
        patch("opi.web.router_user_admin._get_otp_service", return_value=otp),
        pytest.raises(HTTPException) as fout,
    ):
        await reset_user_otp(_verzoek(BEHEERDER), USER_ID)

    assert fout.value.status_code == 403
    otp.reset.assert_not_awaited()


async def test_een_onbekende_gebruiker_geeft_404() -> None:
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst({BEHEERDER})),
        patch("opi.web.router_user_admin._get_service", return_value=_gebruikersbeheer(None)),
        pytest.raises(HTTPException) as fout,
    ):
        await reset_user_otp(_verzoek(BEHEERDER), USER_ID)

    assert fout.value.status_code == 404
