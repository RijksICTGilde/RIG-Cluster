"""De ``requires_step_up``-decorator: hoe een handeling zich aanmeldt (RC-332).

Twee wegen: een API-aanroep (pad onder ``/api/``) krijgt een probleemdetail, het
portaal krijgt een dialoog bij htmx en anders een kale 403. Is de conditie onwaar,
dan gebeurt er niets extra.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.auth_decorators import REQUIRES_STEP_UP, requires_step_up
from opi.core.step_up import StepUpActie

ACTIE = StepUpActie.PROJECT_VERWIJDEREN
HANDELING = ACTIE.handeling


def _request(path: str, *, htmx: bool = False, email: str = "eigenaar@example.org") -> SimpleNamespace:
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        headers={"HX-Request": "true"} if htmx else {},
        state=SimpleNamespace(user={"email": email}),
    )


def _decorated(condition: Any) -> Any:
    @requires_step_up(condition, actie=ACTIE)
    async def route(request: Any, project_name: str = "demo") -> str:
        return "uitgevoerd"

    return route


# ---------------------------------------------------------------------------
# Conditie onwaar: niets extra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conditie_onwaar_voert_de_handeling_meteen_uit() -> None:
    route = _decorated(lambda request, **kwargs: False)
    resultaat = await route(_request("/projects/delete/demo"), project_name="demo")
    assert resultaat == "uitgevoerd"


@pytest.mark.asyncio
async def test_een_async_conditie_wordt_ook_afgewacht() -> None:
    async def conditie(request: Any, **kwargs: Any) -> bool:
        return False

    route = _decorated(conditie)
    assert await route(_request("/projects/delete/demo"), project_name="demo") == "uitgevoerd"


def test_de_decorator_markeert_de_functie_met_de_actie() -> None:
    route = _decorated(lambda request, **kwargs: False)
    assert getattr(route, REQUIRES_STEP_UP) is ACTIE


# ---------------------------------------------------------------------------
# API-pad: probleemdetail, geen dialoog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_pad_zonder_geldige_otp_geeft_het_probleem_en_voert_niets_uit() -> None:
    route = _decorated(lambda request, **kwargs: True)
    probleem = MagicMock()
    antwoord = object()

    with (
        patch("opi.api.otp_step_up.verify_api_step_up", new=AsyncMock(return_value=probleem)) as verify,
        patch("opi.api.otp_step_up.step_up_problem_response", return_value=antwoord) as respond,
    ):
        resultaat = await route(_request("/api/projects/demo"), project_name="demo")

    assert resultaat is antwoord
    respond.assert_called_once_with(probleem)
    verify.assert_awaited_once_with(ANY, project_name="demo", actie=ACTIE, doelwit="demo")


@pytest.mark.asyncio
async def test_api_pad_kiest_naam_als_doelwit_boven_het_project() -> None:
    """``naam`` is het lid van de ledenroutes (RC-387): het doelwit, niet het project."""

    @requires_step_up(lambda request, **kwargs: True, actie=ACTIE)
    async def route(request: Any, project_name: str, naam: str) -> str:
        return "uitgevoerd"

    with patch("opi.api.otp_step_up.verify_api_step_up", new=AsyncMock(return_value=None)) as verify:
        await route(_request("/api/projects/demo/leden/bouwstraat"), project_name="demo", naam="bouwstraat")

    verify.assert_awaited_once_with(ANY, project_name="demo", actie=ACTIE, doelwit="bouwstraat")


@pytest.mark.asyncio
async def test_api_pad_met_geldige_otp_voert_de_handeling_uit() -> None:
    route = _decorated(lambda request, **kwargs: True)

    with patch("opi.api.otp_step_up.verify_api_step_up", new=AsyncMock(return_value=None)):
        resultaat = await route(_request("/api/projects/demo"), project_name="demo")

    assert resultaat == "uitgevoerd"


# ---------------------------------------------------------------------------
# Portaalpad: dialoog bij htmx, kale 403 voor al het andere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portaal_zonder_verse_step_up_en_zonder_htmx_geeft_een_kale_403() -> None:
    route = _decorated(lambda request, **kwargs: True)

    with (
        patch("opi.core.auth_decorators.has_fresh_step_up", return_value=False),
        pytest.raises(HTTPException) as excinfo,
    ):
        await route(_request("/projects/delete/demo"), project_name="demo")

    assert excinfo.value.status_code == 403
    assert HANDELING in excinfo.value.detail


@pytest.mark.asyncio
async def test_portaal_zonder_verse_step_up_met_htmx_geeft_de_dialoog() -> None:
    route = _decorated(lambda request, **kwargs: True)
    dialoog_response = object()

    with (
        patch("opi.core.auth_decorators.has_fresh_step_up", return_value=False),
        patch("opi.web.step_up_dialog.render_step_up_dialog", new=AsyncMock(return_value="<html/>")) as render,
        patch("opi.web.step_up_dialog.step_up_dialog_response", return_value=dialoog_response) as respond,
    ):
        resultaat = await route(_request("/projects/delete/demo", htmx=True), project_name="demo")

    assert resultaat is dialoog_response
    render.assert_awaited_once()
    respond.assert_called_once_with("<html/>")


@pytest.mark.asyncio
async def test_portaal_met_verse_step_up_voert_de_handeling_uit() -> None:
    route = _decorated(lambda request, **kwargs: True)

    with patch("opi.core.auth_decorators.has_fresh_step_up", return_value=True):
        resultaat = await route(_request("/projects/delete/demo"), project_name="demo")

    assert resultaat == "uitgevoerd"


@pytest.mark.asyncio
async def test_portaal_geeft_de_deployment_naam_mee_aan_de_dialoog_niet_het_project() -> None:
    """``doelwit`` kiest de deployment- of componentnaam boven de projectnaam."""

    @requires_step_up(lambda request, **kwargs: True, actie=StepUpActie.DEPLOYMENT_VERWIJDEREN)
    async def route(request: Any, project_name: str, deployment_name: str) -> str:
        return "uitgevoerd"

    with (
        patch("opi.core.auth_decorators.has_fresh_step_up", return_value=False),
        patch("opi.web.step_up_dialog.render_step_up_dialog", new=AsyncMock(return_value="<html/>")) as render,
        patch("opi.web.step_up_dialog.step_up_dialog_response", return_value=MagicMock()),
    ):
        await route(
            _request("/projects/demo/delete-deployment/prod", htmx=True), project_name="demo", deployment_name="prod"
        )

    render.assert_awaited_once_with(
        ANY,
        vervolg="/projects/demo/delete-deployment/prod",
        handeling=StepUpActie.DEPLOYMENT_VERWIJDEREN.handeling,
        vervolg_velden={},
    )
