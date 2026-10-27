"""De route die het portaal om een verse step-up vraagt (RC-332).

Dekt ``opi/web/router_step_up.py``: een geldige code zet de stempel en herhaalt de
oorspronkelijke handeling, een foute of geblokkeerde code doet dat niet.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from opi.core.step_up import STEP_UP_SESSION_KEY
from opi.services.user_otp_service import OtpVerification
from opi.web.router_step_up import step_up

EMAIL = "eigenaar@example.org"


def _request(form: dict[str, str], *, email: str = EMAIL) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session={},
        form=AsyncMock(return_value=form),
    )


def _service(*, verify: OtpVerification = OtpVerification.OK) -> AsyncMock:
    service = AsyncMock()
    service.verify = AsyncMock(return_value=verify)
    return service


@pytest.mark.asyncio
async def test_geldige_code_zet_de_step_up_en_herhaalt_de_handeling(caplog: pytest.LogCaptureFixture) -> None:
    request = _request(
        {
            "vervolg": "/projects/demo/modal-wizard/modal-edit-identity/confirm",
            "handeling": "dit project te verwijderen",
            "code": "123456",
            "vervolg_token": "wt-123",
        }
    )
    service = _service(verify=OtpVerification.OK)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_repeat", return_value="<herhaal/>") as herhaal,
        caplog.at_level("INFO", logger="opi.web.router_step_up"),
    ):
        response = await step_up(request)

    assert STEP_UP_SESSION_KEY in request.session
    herhaal.assert_called_once_with(
        request,
        vervolg="/projects/demo/modal-wizard/modal-edit-identity/confirm",
        vervolg_token="wt-123",
        vervolg_velden={},
        methode="POST",
    )
    assert response.body == b"<herhaal/>"
    meldingen = [record.getMessage() for record in caplog.records]
    assert any("Step-up geslaagd voor" in m and EMAIL in m for m in meldingen)


@pytest.mark.asyncio
async def test_de_herhaling_neemt_de_methode_van_de_handeling_over() -> None:
    """Een handeling die een GET is (het env-varsvenster, RC-388) herhaalt als GET."""
    request = _request(
        {
            "vervolg": "/projects/demo/componenten/web/env-vars",
            "handeling": "de omgevingsvariabelen te zien en te wijzigen",
            "code": "123456",
            "vervolg_methode": "GET",
        }
    )
    service = _service(verify=OtpVerification.OK)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_repeat", return_value="<herhaal/>") as herhaal,
    ):
        await step_up(request)

    assert herhaal.call_args.kwargs["methode"] == "GET"


@pytest.mark.asyncio
async def test_een_onbekende_methode_valt_terug_op_post() -> None:
    request = _request(
        {
            "vervolg": "/projects/demo/componenten/web/env-vars",
            "handeling": "iets",
            "code": "123456",
            "vervolg_methode": "DELETE",
        }
    )
    service = _service(verify=OtpVerification.OK)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_repeat", return_value="<herhaal/>") as herhaal,
    ):
        await step_up(request)

    assert herhaal.call_args.kwargs["methode"] == "POST"


@pytest.mark.asyncio
async def test_foute_code_behoudt_de_methode_voor_de_herhaling() -> None:
    """Een foute code op een GET-handeling (RC-388) rendert de dialoog opnieuw mét GET:
    anders postte de volgende poging naar een route die alleen GET kent."""
    request = _request(
        {
            "vervolg": "/projects/demo/componenten/web/env-vars",
            "handeling": "de omgevingsvariabelen te zien en te wijzigen",
            "code": "000000",
            "vervolg_methode": "GET",
        }
    )
    service = _service(verify=OtpVerification.INVALID)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_dialog", new=AsyncMock(return_value="<dialoog/>")) as dialoog,
    ):
        await step_up(request)

    assert STEP_UP_SESSION_KEY not in request.session
    assert dialoog.call_args.kwargs["methode"] == "GET"


@pytest.mark.asyncio
async def test_foute_code_zet_niets_en_toont_de_foutmelding() -> None:
    request = _request({"vervolg": "/x", "handeling": "iets", "code": "000000"})
    service = _service(verify=OtpVerification.INVALID)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_dialog", new=AsyncMock(return_value="<dialoog/>")) as dialoog,
    ):
        response = await step_up(request)

    assert STEP_UP_SESSION_KEY not in request.session
    dialoog.assert_awaited_once()
    assert dialoog.call_args.kwargs["fout"] == "Die code klopt niet. Vul de code in die je app nu toont."
    assert response.body == b"<dialoog/>"


@pytest.mark.asyncio
async def test_geblokkeerd_toont_de_blokkadetekst_en_zet_niets() -> None:
    request = _request({"vervolg": "/x", "handeling": "iets", "code": "000000"})
    service = _service(verify=OtpVerification.LOCKED)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_dialog", new=AsyncMock(return_value="<dialoog/>")) as dialoog,
    ):
        await step_up(request)

    assert STEP_UP_SESSION_KEY not in request.session
    assert "geblokkeerd" in dialoog.call_args.kwargs["fout"]


@pytest.mark.asyncio
async def test_zonder_bevestigde_factor_zet_de_route_niets() -> None:
    """De factor uit RC-329 kent geen herstelcodes: alleen een geldige TOTP telt."""
    request = _request({"vervolg": "/x", "handeling": "iets", "code": "123456"})
    service = _service(verify=OtpVerification.NOT_REGISTERED)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_dialog", new=AsyncMock(return_value="<dialoog/>")),
    ):
        await step_up(request)

    assert STEP_UP_SESSION_KEY not in request.session


@pytest.mark.asyncio
async def test_handeling_uit_het_formulier_forgeert_geen_logregel(caplog: pytest.LogCaptureFixture) -> None:
    """Een regeleinde in ``handeling`` zou anders een eigen logregel worden.

    De log is de herleidbaarheid die deze poort moet leveren (BIO2 8.32), dus een
    inzender mag er geen geslaagde step-up van iemand anders in kunnen schrijven.
    """
    forged = "x\n2026-10-22 14:00:00 - opi.web.router_step_up - INFO - Step-up geslaagd voor beheerder@example.org"
    request = _request({"vervolg": "/x", "handeling": forged, "code": "000000"})
    service = _service(verify=OtpVerification.INVALID)

    with (
        patch("opi.web.router_step_up.UserOtpService", return_value=service),
        patch("opi.web.router_step_up.render_step_up_dialog", new=AsyncMock(return_value="<dialoog/>")) as dialoog,
        caplog.at_level("WARNING", logger="opi.web.router_step_up"),
    ):
        await step_up(request)

    melding = next(r.getMessage() for r in caplog.records if "Step-up geweigerd" in r.getMessage())
    assert "\n" not in melding
    assert "Step-up geslaagd" not in melding
    assert "\n" not in dialoog.call_args.kwargs["handeling"]
