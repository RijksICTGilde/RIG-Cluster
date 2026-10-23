"""De API-weg voor het verwijderen van een project draagt de step-up (RC-332).

Eén route, end-to-end door ``validate_api_token`` en ``requires_step_up`` heen. Alle
step-up-routes van de API worden vandaag met de PROJECTSLEUTEL afgeschermd, en die hoort
bij geen gebruiker: zo'n handeling kan daar dus niet, ook niet met een code erbij. De
andere API-routes uit de tabel dragen dezelfde twee decorators in dezelfde volgorde
(bewaakt door ``test_step_up_routes.py``) en roepen dezelfde ``verify_api_step_up``- en
conditiefuncties aan, die in ``test_otp_step_up.py`` en ``test_step_up_conditions.py`` los
staan getoetst; dit bestand bewijst dat de twee lagen ook ECHT op elkaar aansluiten, niet
dat elke route dat apart nog eens doet.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.otp_step_up import STEP_UP_PROBLEM_TYPE, StepUpCause
from opi.api.router import delete_project
from opi.core.step_up import StepUpActie
from opi.services.user_otp_service import OtpStatus, OtpVerification

API_KEY = "sleutel-123"
LID = "lid@example.org"


def _project_record(data: dict[str, Any]) -> MagicMock:
    record = MagicMock()
    record.data = data
    record.api_key = API_KEY
    record.name = data["name"]
    return record


def _request(headers: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        headers=headers,
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/projects/demo"),
    )


def _delete_data() -> MagicMock:
    return MagicMock(confirmDeletion=True, force=False)


def _patches(data: dict[str, Any]):
    record = _project_record(data)
    manager = MagicMock()
    manager.delete_project = AsyncMock(return_value={"success": True})
    manager.close = AsyncMock(return_value=None)
    return (
        patch("opi.api.endpoint_util.get_project_store", return_value=MagicMock(get=MagicMock(return_value=record))),
        patch(
            "opi.services.step_up_conditions.get_project_store",
            return_value=MagicMock(get=MagicMock(return_value=record)),
        ),
        patch("opi.services.step_up_conditions.is_user_authorized_for_project", return_value=True),
        patch("opi.api.router.create_project_manager", return_value=manager),
        manager,
    )


NIET_PRODUCTIE = {"name": "demo", "deployments": [{"name": "acc", "environment": "acceptatie"}]}
PRODUCTIE = {"name": "demo", "deployments": [{"name": "prod", "environment": "productie"}]}


@pytest.mark.asyncio
async def test_niet_productie_met_de_projectsleutel_zonder_otp_slaagt() -> None:
    p1, p2, p3, p4, manager = _patches(NIET_PRODUCTIE)
    with p1, p2, p3, p4:
        response = await delete_project(
            request=_request({"X-API-Key": API_KEY}), project_name="demo", delete_data=_delete_data()
        )

    assert response.status_code == 200
    manager.delete_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_productie_met_de_projectsleutel_zonder_otp_weigert_en_verwijdert_niets() -> None:
    p1, p2, p3, p4, manager = _patches(PRODUCTIE)
    with p1, p2, p3, p4:
        response = await delete_project(
            request=_request({"X-API-Key": API_KEY}), project_name="demo", delete_data=_delete_data()
        )

    assert response.status_code == 403
    assert response.media_type == "application/problem+json"
    probleem = json.loads(response.body)
    assert probleem["type"] == STEP_UP_PROBLEM_TYPE
    assert probleem["cause"] == StepUpCause.TOKENSOORT_NIET_TOEGESTAAN.value
    assert probleem["action"] == StepUpActie.PROJECT_VERWIJDEREN.value
    manager.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_productie_met_de_projectsleutel_en_een_code_weigert_ook() -> None:
    """Een machine heeft geen tweede factor; een code aan een projectsleutel hangen mag niet."""
    p1, p2, p3, p4, manager = _patches(PRODUCTIE)
    with p1, p2, p3, p4, patch("opi.api.otp_step_up.UserOtpService") as service_cls:
        response = await delete_project(
            request=_request({"X-API-Key": API_KEY, "X-ZAD-OTP": "123456"}),
            project_name="demo",
            delete_data=_delete_data(),
        )

    assert response.status_code == 403
    assert json.loads(response.body)["cause"] == StepUpCause.TOKENSOORT_NIET_TOEGESTAAN.value
    service_cls.assert_not_called()
    manager.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_productie_met_de_eigen_code_van_een_persoonlijk_token_slaagt() -> None:
    """De weg die open blijft: de code van de eigenaar van het token.

    Vandaag draagt geen step-up-route ``validate_user_token``, dus hier staat
    ``request.state.user`` zoals die decorator hem zou zetten.
    """
    p1, p2, p3, p4, manager = _patches(PRODUCTIE)
    request = _request({"X-API-Key": API_KEY, "X-ZAD-OTP": "654321"})
    request.state.user = {"email": LID}
    service = MagicMock(verify=AsyncMock(return_value=OtpVerification.OK))
    with p1, p2, p3, p4, patch("opi.api.otp_step_up.UserOtpService", return_value=service):
        response = await delete_project(request=request, project_name="demo", delete_data=_delete_data())

    assert response.status_code == 200
    service.verify.assert_awaited_once_with(LID, "654321")
    manager.delete_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_productie_met_een_ongeldige_code_van_de_eigenaar_weigert() -> None:
    p1, p2, p3, p4, manager = _patches(PRODUCTIE)
    request = _request({"X-API-Key": API_KEY, "X-ZAD-OTP": "000000"})
    request.state.user = {"email": LID}
    service = MagicMock(
        verify=AsyncMock(return_value=OtpVerification.INVALID),
        status=AsyncMock(return_value=OtpStatus(registered=True)),
    )
    with p1, p2, p3, p4, patch("opi.api.otp_step_up.UserOtpService", return_value=service):
        response = await delete_project(request=request, project_name="demo", delete_data=_delete_data())

    assert response.status_code == 403
    assert json.loads(response.body)["cause"] == StepUpCause.CODE_ONGELDIG.value
    manager.delete_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_zonder_projectsleutel_komt_niet_tot_de_step_up() -> None:
    """``validate_api_token`` gaat voor: geen sleutel, geen poging tot step-up."""
    p1, p2, p3, p4, manager = _patches(PRODUCTIE)
    with p1, p2, p3, p4, pytest.raises(HTTPException) as excinfo:
        await delete_project(request=_request({}), project_name="demo", delete_data=_delete_data())

    assert excinfo.value.status_code == 401
    manager.delete_project.assert_not_awaited()
