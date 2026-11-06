"""De step-up bij een API-aanroep die productie raakt (RC-332).

Dekt ``opi/api/otp_step_up.py``: de projectsleutel kan deze handeling nooit, een
persoonlijk token wel met een step-up van de EIGENAAR van dat token, en het problem-detail
zegt per geval wat er moet gebeuren zonder te verklappen welk deel van een code fout was.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.otp_step_up import (
    OTP_ENROLL_PATH,
    STEP_UP_PROBLEM_TYPE,
    StepUpCause,
    StepUpProblem,
    step_up_problem_response,
    verify_api_step_up,
    verify_step_up_code,
)
from opi.core.step_up import StepUpAction
from opi.services.user_otp_service import OtpStatus, OtpVerification

PROJECT = "demo"
EIGENAAR = "eigenaar@example.org"
ACTIE = StepUpAction.DEPLOYMENT_DELETE


def _request(*, owner: str | None = None, fresh: bool = False) -> SimpleNamespace:
    state = SimpleNamespace(user={"email": owner} if owner else None, step_up_fresh=fresh)
    return SimpleNamespace(headers={}, state=state, url=SimpleNamespace(path=f"/api/projects/{PROJECT}"))


async def _verify(request: SimpleNamespace) -> StepUpProblem | None:
    return await verify_api_step_up(request, project_name=PROJECT, actie=ACTIE, doelwit="prod")


def _otp_service(uitkomst: OtpVerification, *, status: OtpStatus | None = None) -> MagicMock:
    service = MagicMock()
    service.verify = AsyncMock(return_value=uitkomst)
    service.status = AsyncMock(return_value=status or OtpStatus(registered=True))
    return service


# ---------------------------------------------------------------------------
# De projectsleutel hoort bij geen gebruiker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projectsleutel_zonder_code_weigert_op_de_tokensoort() -> None:
    probleem = await _verify(_request())
    assert probleem is not None
    assert probleem.cause is StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED
    assert "persoonlijk token" in probleem.remedy


@pytest.mark.asyncio
async def test_a_fresh_state_does_not_open_a_key_without_a_user(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="opi.api.otp_step_up"):
        probleem = await _verify(_request(fresh=True))

    assert probleem is not None
    assert probleem.cause is StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED
    assert any("sleutel zonder gebruiker" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_key_without_a_user_cannot_activate_a_step_up() -> None:
    with patch("opi.api.otp_step_up.UserOtpService") as service_cls:
        probleem = await verify_step_up_code(_request(), "123456")

    assert probleem is not None
    assert probleem.cause is StepUpCause.PRINCIPAL_KIND_NOT_ALLOWED
    service_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Een persoonlijk token: de code is van de eigenaar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_step_up_the_caller_is_told_to_activate_one() -> None:
    probleem = await _verify(_request(owner=EIGENAAR))
    assert probleem is not None
    assert probleem.cause is StepUpCause.OTP_MISSING
    assert "/api/v2/step-up" in probleem.remedy


@pytest.mark.asyncio
async def test_a_fresh_step_up_lets_the_owner_through_without_a_code(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch("opi.api.otp_step_up.UserOtpService") as service_cls,
        caplog.at_level("INFO", logger="opi.api.otp_step_up"),
    ):
        probleem = await _verify(_request(owner=EIGENAAR, fresh=True))

    assert probleem is None
    service_cls.assert_not_called()
    assert any("Verse step-up gebruikt" in r.getMessage() and EIGENAAR in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_code_is_checked_against_the_owner_of_the_token() -> None:
    service = _otp_service(OtpVerification.OK)
    with patch("opi.api.otp_step_up.UserOtpService", return_value=service):
        assert await verify_step_up_code(_request(owner=EIGENAAR), " 654321 ") is None

    service.verify.assert_awaited_once_with(EIGENAAR, "654321")


@pytest.mark.asyncio
async def test_zonder_bevestigde_factor_wijst_het_antwoord_de_weg() -> None:
    with patch("opi.api.otp_step_up.UserOtpService", return_value=_otp_service(OtpVerification.NOT_REGISTERED)):
        probleem = await verify_step_up_code(_request(owner=EIGENAAR), "123456")

    assert probleem is not None
    assert probleem.cause is StepUpCause.FACTOR_MISSING
    assert probleem.enroll_url == OTP_ENROLL_PATH


@pytest.mark.asyncio
async def test_een_ongeldige_of_hergebruikte_code_geeft_een_reden_zonder_details() -> None:
    with patch("opi.api.otp_step_up.UserOtpService", return_value=_otp_service(OtpVerification.INVALID)):
        probleem = await verify_step_up_code(_request(owner=EIGENAAR), "000000")

    assert probleem is not None
    assert probleem.cause is StepUpCause.CODE_INVALID
    assert probleem.blocked_until is None
    assert "000000" not in probleem.detail


@pytest.mark.asyncio
async def test_geblokkeerd_zegt_wanneer_het_weer_mag() -> None:
    tot = datetime.now(UTC) + timedelta(minutes=10)
    service = _otp_service(OtpVerification.LOCKED, status=OtpStatus(registered=True, locked_until=tot))
    with patch("opi.api.otp_step_up.UserOtpService", return_value=service):
        probleem = await verify_step_up_code(_request(owner=EIGENAAR), "000000")

    assert probleem is not None
    assert probleem.cause is StepUpCause.BLOCKED
    assert probleem.blocked_until == tot.isoformat()


# ---------------------------------------------------------------------------
# Het antwoord zelf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_het_probleem_noemt_de_handeling_en_waarom() -> None:
    probleem = await _verify(_request(owner=EIGENAAR))
    assert probleem is not None
    assert probleem.type == STEP_UP_PROBLEM_TYPE
    assert probleem.status == 403
    assert probleem.action == "deployment-delete"
    assert probleem.reason == "de deployment 'prod' staat op productie"
    assert probleem.instance == f"/api/projects/{PROJECT}"


@pytest.mark.asyncio
async def test_het_antwoord_is_een_403_problem_json() -> None:
    probleem = await _verify(_request(owner=EIGENAAR))
    assert probleem is not None
    response = step_up_problem_response(probleem)

    assert response.status_code == 403
    assert response.media_type == "application/problem+json"
    assert STEP_UP_PROBLEM_TYPE in response.body.decode()
