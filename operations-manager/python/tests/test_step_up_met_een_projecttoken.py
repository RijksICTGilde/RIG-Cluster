"""De step-up op een ``zad_``-token, end-to-end door de echte ingang (RC-322 op RC-332).

RC-332 bouwde de step-up op de API en kon hem alleen met een handgezette
``request.state.user`` meten: geen enkele route droeg toen een bewijsmiddel dat bij een
PERSOON hoort. Sinds deze tak doet een ``zad_``-token dat wel, en dan is er iets anders te
toetsen dan daar: niet of ``verify_api_step_up`` klopt (dat staat in
``test_otp_step_up.py``), maar of de EIGENAAR van het token echt de persoon is tegen wie
de code wordt getoetst, met een token dat als token binnenkomt.

Vijf uitkomsten, over de echte ``delete_project``, dus door ``validate_api_token``,
``handhaaf`` en ``requires_step_up`` heen:

1. token van een mens, productie, zonder code  -> 403 ``otp-missing``
2. token van een mens, productie, geldige code -> door, en de code is tegen HEM getoetst
3. token van een mens, productie, foute code   -> 403 ``code-invalid``
4. token van een service account, ook met code -> 403 ``principal-kind-not-allowed``
5. de oude projectsleutel, ook met code        -> 403 ``principal-kind-not-allowed``

De laatste twee raken de tweede factor niet aan: een machine heeft er geen, dus
``UserOtpService`` hoort daar niet eens aangeroepen te worden.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.otp_step_up import STEP_UP_PROBLEM_TYPE, StepUpCause
from opi.api.router import delete_project
from opi.api.step_up_router import StepUpRequest, activate_token_step_up
from opi.core.leden import LidSoort
from opi.core.step_up import StepUpAction
from opi.services.api_step_up_service import personal_token_credential
from opi.services.project_leden import hash_van
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser
from opi.services.user_otp_service import OtpStatus, OtpVerification
from opi.services.user_service import get_user_service
from starlette.requests import Request

LID = "lid@rijksoverheid.nl"
TOKEN_MENS = "zad_vaneenmens"
TOKEN_BOUWSTRAAT = "zad_vaneenbouwstraat"
OUDE_SLEUTEL = "de-oude-projectsleutel"

#: Productie, want alleen daar vraagt het verwijderen van een project een step-up.
PRODUCTIE: dict[str, Any] = {"name": "demo", "deployments": [{"name": "prod", "environment": "productie"}]}


def _token(naam: str, waarde: str) -> ProjectToken:
    return ProjectToken(name=naam, expires=datetime.now(UTC) + timedelta(days=30), hash=hash_van(waarde))


def _project(*, met_oude_sleutel: bool = False) -> ProjectSummary:
    return ProjectSummary(
        name="demo",
        api_key=OUDE_SLEUTEL if met_oude_sleutel else None,
        filename="projects/demo.yaml",
        users=[
            ProjectUser(email=LID, role="admin", tokens=[_token("cli", TOKEN_MENS)]),
            ProjectUser(
                type=LidSoort.SERVICE_ACCOUNT,
                name="project-api",
                role="admin",
                tokens=[_token("pijplijn", TOKEN_BOUWSTRAAT)],
            ),
        ],
        data=PRODUCTIE,
    )


def _verzoek(headers: dict[str, str]) -> Request:
    """Een echt ``Request``, want ``handhaaf`` schrijft de eigenaar op ``state`` en dat is
    precies wat hier gemeten wordt."""
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "path": "/api/v1/projects/demo",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("10.0.0.1", 1234),
        }
    )


@pytest.fixture(autouse=True)
def _lid_heeft_platformtoegang():
    """Een token is niet meer dan de persoon: zonder platformtoegang is het 401."""
    dienst = get_user_service()
    dienst.add_allowed_email(LID)
    yield
    dienst.remove_allowed_email(LID)


def _patches(project: ProjectSummary):
    store = MagicMock(get=MagicMock(return_value=project))
    manager = MagicMock()
    manager.delete_project = AsyncMock(return_value={"success": True})
    manager.close = AsyncMock(return_value=None)
    return (
        patch("opi.api.endpoint_util.get_project_store", return_value=store),
        patch("opi.services.step_up_conditions.get_project_store", return_value=store),
        patch("opi.services.step_up_conditions.is_user_authorized_for_project", return_value=True),
        patch("opi.api.router.create_project_manager", return_value=manager),
        manager,
    )


async def _verwijder(headers: dict[str, str], *, project: ProjectSummary, fresh: bool = False):
    p1, p2, p3, p4, manager = _patches(project)
    with (
        p1,
        p2,
        p3,
        p4,
        patch("opi.api.endpoint_util.ApiStepUpService", return_value=MagicMock(is_fresh=AsyncMock(return_value=fresh))),
    ):
        antwoord = await delete_project(
            request=_verzoek(headers),
            project_name="demo",
            delete_data=MagicMock(confirmDeletion=True, force=False),
        )
    return antwoord, manager


def _probleem(antwoord) -> dict[str, Any]:
    assert antwoord.status_code == 403
    assert antwoord.media_type == "application/problem+json"
    inhoud = json.loads(antwoord.body)
    assert inhoud["type"] == STEP_UP_PROBLEM_TYPE
    assert inhoud["action"] == StepUpAction.PROJECT_DELETE.value
    return inhoud


@pytest.mark.asyncio
async def test_een_token_van_een_mens_zonder_code_weigert_met_een_problem_detail() -> None:
    antwoord, manager = await _verwijder({"X-API-Key": TOKEN_MENS}, project=_project())

    inhoud = _probleem(antwoord)
    assert inhoud["cause"] == StepUpCause.OTP_MISSING.value
    manager.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_personal_token_with_a_fresh_step_up_goes_through() -> None:
    antwoord, manager = await _verwijder({"X-API-Key": TOKEN_MENS}, project=_project(), fresh=True)

    assert antwoord.status_code == 200
    manager.delete_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_service_account_cannot_have_a_step_up() -> None:
    antwoord, manager = await _verwijder({"X-API-Key": TOKEN_BOUWSTRAAT}, project=_project(), fresh=True)

    assert _probleem(antwoord)["cause"] == StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED.value
    manager.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_old_project_key_cannot_have_a_step_up() -> None:
    antwoord, manager = await _verwijder(
        {"X-API-Key": OUDE_SLEUTEL}, project=_project(met_oude_sleutel=True), fresh=True
    )

    assert _probleem(antwoord)["cause"] == StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED.value
    manager.delete_project.assert_not_awaited()


async def _activate(headers: dict[str, str], *, verification: OtpVerification = OtpVerification.OK) -> tuple[Any, Any]:
    """Through the real gate of the route: recognising the token, the rule and the code."""
    project = _project()
    otp = MagicMock(
        verify=AsyncMock(return_value=verification), status=AsyncMock(return_value=OtpStatus(registered=True))
    )
    service = MagicMock(
        is_fresh=AsyncMock(return_value=False), record=AsyncMock(return_value=datetime.now(UTC) + timedelta(minutes=5))
    )
    store = MagicMock(get=MagicMock(return_value=project))
    with (
        patch("opi.api.endpoint_util.get_project_store", return_value=store),
        patch("opi.api.endpoint_util.ApiStepUpService", return_value=service),
        patch("opi.api.step_up_router.ApiStepUpService", return_value=service),
        patch("opi.api.step_up_router.record_step_up_attempt", AsyncMock()),
        patch("opi.api.otp_step_up.UserOtpService", return_value=otp),
    ):
        response = await activate_token_step_up(
            request=_verzoek(headers), project_name="demo", body=StepUpRequest(code="123456")
        )
    return response, service


@pytest.mark.asyncio
async def test_a_personal_token_activates_a_step_up_for_itself() -> None:
    response, service = await _activate({"X-API-Key": TOKEN_MENS})

    assert response.status_code == 200
    service.record.assert_awaited_once_with(personal_token_credential(TOKEN_MENS), LID)


@pytest.mark.asyncio
async def test_a_wrong_code_records_nothing_for_a_personal_token() -> None:
    response, service = await _activate({"X-API-Key": TOKEN_MENS}, verification=OtpVerification.INVALID)

    assert json.loads(response.body)["cause"] == StepUpCause.CODE_INVALID.value
    service.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_service_account_token_cannot_activate_a_step_up() -> None:
    with pytest.raises(HTTPException) as refused:
        await _activate({"X-API-Key": TOKEN_BOUWSTRAAT})

    assert refused.value.status_code == 403
