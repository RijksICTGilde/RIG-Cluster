"""Een token uitgeven, roteren of intrekken vraagt een verse OTP (RC-387).

Per route, end-to-end door ``validate_user_token`` en ``requires_step_up`` heen: het
bearertoken legitimeert de persoon, de code bewijst dat het echt hij is, nu. De step-up
zelf staat los getoetst in ``test_otp_step_up.py`` en de regels van de routes in
``test_token_router_schrijven.py``; dit bestand bewijst dat de twee lagen op elke
schrijvende ledenroute op elkaar aansluiten, en dat de leesroute er geen vraagt.

Dezelfde opzet als ``test_platformbeheer_step_up.py``, maar langs de echte HTTP-laag: deze
routes dragen een body en een pad met het lid erin, en dat is waar de weigering over gaat.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.otp_step_up import STEP_UP_PROBLEM_MEDIA_TYPE, STEP_UP_PROBLEM_TYPE, StepUpCause
from opi.core.step_up import StepUpAction
from opi.services.api_step_up_service import sso_session_credential
from opi.services.user_otp_service import OtpStatus, OtpVerification

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

PROJECT = "bouwput"
LID = "bouwstraat"

#: De zes schrijvende handelingen, met de dienst die ze aanroepen en de actie die ze in het
#: problem-detail noemen. De naam is de sleutel waarop de parametrisatie leest.
ROUTES: dict[str, tuple[str, str, dict[str, Any] | None, str, StepUpAction]] = {
    "service-account-add": (
        "POST",
        f"/api/v2/projects/{PROJECT}/leden",
        {"naam": LID, "roles": ["developer"]},
        "voeg_service_account_toe",
        StepUpAction.SERVICE_ACCOUNT_ADD,
    ),
    "service-account-remove": (
        "DELETE",
        f"/api/v2/projects/{PROJECT}/leden/{LID}",
        None,
        "verwijder_service_account",
        StepUpAction.SERVICE_ACCOUNT_REMOVE,
    ),
    "role-change": (
        "PUT",
        f"/api/v2/projects/{PROJECT}/leden/{LID}/rol",
        {"roles": ["admin"]},
        "set_member_roles",
        StepUpAction.ROLE_CHANGE,
    ),
    "token-issue": (
        "POST",
        f"/api/v2/projects/{PROJECT}/leden/{LID}/tokens",
        {"naam": "pijplijn", "dagen": 30},
        "geef_token_uit",
        StepUpAction.TOKEN_ISSUE,
    ),
    "token-rotate": (
        "POST",
        f"/api/v2/projects/{PROJECT}/leden/{LID}/tokens/pijplijn/:roteer",
        {"naam": "pijplijn", "dagen": 30},
        "roteer_token",
        StepUpAction.TOKEN_ROTATE,
    ),
    "token-revoke": (
        "DELETE",
        f"/api/v2/projects/{PROJECT}/leden/{LID}/tokens/pijplijn",
        None,
        "trek_token_in",
        StepUpAction.TOKEN_REVOKE,
    ),
}


@pytest.fixture(autouse=True)
def diensten() -> Iterator[dict[str, AsyncMock]]:
    """Alle ledendiensten als mock: hier gaat het om de step-up, niet om de commit."""
    mocks = {naam: AsyncMock(return_value="zad_nieuw") for _, _, _, naam, _ in ROUTES.values()}
    with patch.multiple("opi.api.token_router", is_user_authorized_for_project=MagicMock(return_value=True), **mocks):
        yield mocks


@pytest.fixture
def otp() -> Iterator[dict[str, Any]]:
    """Een code die een keer wordt aangenomen, met de adressen waartegen is getoetst."""
    getoetst: list[tuple[str, str]] = []

    async def _verify(email: str, code: str) -> OtpVerification:
        getoetst.append((email, code))
        return OtpVerification.OK if code == "123456" else OtpVerification.INVALID

    dienst = MagicMock(verify=AsyncMock(side_effect=_verify), status=AsyncMock(return_value=OtpStatus(registered=True)))
    with (
        patch("opi.api.otp_step_up.UserOtpService", return_value=dienst),
        patch("opi.api.step_up_router.record_step_up_attempt", AsyncMock()),
        patch("opi.services.api_step_up_service.ApiStepUpService.record", AsyncMock(return_value=datetime.now(UTC))),
    ):
        yield {"getoetst": getoetst}


@pytest.fixture
def zonder_factor() -> Iterator[None]:
    """Een gebruiker die zijn tweede factor nog niet heeft ingericht."""
    dienst = MagicMock(
        verify=AsyncMock(return_value=OtpVerification.NOT_REGISTERED),
        status=AsyncMock(return_value=OtpStatus(registered=False)),
    )
    with (
        patch("opi.api.otp_step_up.UserOtpService", return_value=dienst),
        patch("opi.api.step_up_router.record_step_up_attempt", AsyncMock()),
    ):
        yield


def _aanroep(client: TestClient, naam: str, headers: dict[str, str]) -> Any:
    methode, pad, body, _, _ = ROUTES[naam]
    return client.request(methode, pad, json=body, headers=headers)


def _probleem(response: Any) -> dict[str, Any]:
    assert response.status_code == 403
    assert response.headers["content-type"].startswith(STEP_UP_PROBLEM_MEDIA_TYPE)
    probleem = response.json()
    assert probleem["type"] == STEP_UP_PROBLEM_TYPE
    return probleem


@pytest.mark.parametrize("naam", sorted(ROUTES))
def test_zonder_otp_weigert_de_route_met_het_problem_detail(
    naam: str, test_client: TestClient, platformbeheerder_token: Any, diensten: dict[str, AsyncMock]
) -> None:
    probleem = _probleem(_aanroep(test_client, naam, platformbeheerder_token["headers_gewone_gebruiker"]))

    assert probleem["cause"] == StepUpCause.OTP_MISSING.value
    assert probleem["action"] == ROUTES[naam][4].value
    assert PROJECT in probleem["reason"]
    if naam != "service-account-add":
        # Het lid zit in het pad, dus is het doelwit: de reden noemt hem, niet alleen het
        # project. service-account-toevoegen draagt het lid in de body, niet in het pad.
        assert LID in probleem["reason"]
    assert probleem["remedy"]
    diensten[ROUTES[naam][3]].assert_not_awaited()


def test_a_wrong_code_activates_no_step_up(
    test_client: TestClient, platformbeheerder_token: Any, otp: dict[str, Any]
) -> None:
    response = test_client.post(
        "/api/v2/step-up", json={"code": "000000"}, headers=platformbeheerder_token["headers_gewone_gebruiker"]
    )

    assert _probleem(response)["cause"] == StepUpCause.CODE_INVALID.value


def test_without_a_factor_the_activation_shows_the_way(
    test_client: TestClient, platformbeheerder_token: Any, zonder_factor: None
) -> None:
    response = test_client.post(
        "/api/v2/step-up", json={"code": "123456"}, headers=platformbeheerder_token["headers_gewone_gebruiker"]
    )

    probleem = _probleem(response)
    assert probleem["cause"] == StepUpCause.FACTOR_MISSING.value
    assert probleem["enroll_url"] == "/account/otp"


def test_the_code_is_checked_against_the_owner_of_the_bearer_token(
    test_client: TestClient, platformbeheerder_token: Any, otp: dict[str, Any]
) -> None:
    response = test_client.post(
        "/api/v2/step-up", json={"code": "123456"}, headers=platformbeheerder_token["headers_gewone_gebruiker"]
    )

    assert response.status_code == 200
    assert otp["getoetst"] == [("iemand@example.test", "123456")]


@pytest.mark.parametrize("naam", sorted(ROUTES))
def test_met_een_verse_step_up_lukt_de_handeling(
    naam: str,
    test_client: TestClient,
    platformbeheerder_token: Any,
    verse_otp: dict[str, Any],
    diensten: dict[str, AsyncMock],
) -> None:
    response = _aanroep(test_client, naam, platformbeheerder_token["headers_gewone_gebruiker"])

    assert response.status_code in (200, 201)
    diensten[ROUTES[naam][3]].assert_awaited_once()


def test_ending_a_step_up_clears_it_for_this_sso_session(test_client: TestClient, platformbeheerder_token: Any) -> None:
    """De DELETE op ``/api/v2/step-up`` (``end_step_up``) had tot nu toe geen enkele toets."""
    with (
        patch("opi.services.api_step_up_service.ApiStepUpService.is_fresh", AsyncMock(return_value=False)),
        patch("opi.services.api_step_up_service.ApiStepUpService.clear", AsyncMock()) as clear,
    ):
        response = test_client.delete("/api/v2/step-up", headers=platformbeheerder_token["headers_gewone_gebruiker"])

    assert response.status_code == 204
    clear.assert_awaited_once_with(sso_session_credential("sso-sessie-1"))


def test_de_leesroute_vraagt_geen_step_up(test_client: TestClient, platformbeheerder_token: Any) -> None:
    """Alleen lezen verandert niets: een code erop zou een handeling verzinnen."""
    with patch("opi.api.token_router.leden_overzicht", return_value=[]):
        response = test_client.get(
            f"/api/v2/projects/{PROJECT}/leden", headers=platformbeheerder_token["headers_gewone_gebruiker"]
        )

    assert response.status_code == 200
