"""De dialoog waarmee het portaal om een verse step-up vraagt (RC-332).

Dekt ``opi/web/step_up_dialog.py``: de context die naar het fragment gaat, en de
HX-headers die het antwoord in de gedeelde modal zetten.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from opi.services.user_otp_service import OtpStatus
from opi.web.step_up_dialog import MODAL_TARGET, render_step_up_dialog, render_step_up_repeat, step_up_dialog_response

EMAIL = "Eigenaar@Example.ORG"


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"email": EMAIL}))


def test_step_up_dialog_response_retarget_naar_de_gedeelde_modal() -> None:
    response = step_up_dialog_response("<html/>")
    assert response.headers["HX-Retarget"] == MODAL_TARGET
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert response.body == b"<html/>"


@pytest.mark.asyncio
async def test_render_step_up_dialog_vraagt_de_status_op_voor_het_lagere_e_mailadres() -> None:
    status = OtpStatus(registered=True, confirmed_at=None)
    with (
        patch("opi.services.user_otp_service.UserOtpService.status", new=AsyncMock(return_value=status)) as mocked,
        patch("opi.web.lotc_switch.render_fragment", return_value="<dialoog/>") as render,
    ):
        html = await render_step_up_dialog(
            _request(), vervolg="/projects/delete/demo", handeling="dit project te verwijderen", fout="oeps"
        )

    mocked.assert_awaited_once_with("eigenaar@example.org")
    assert html == "<dialoog/>"
    context = render.call_args.kwargs["context"]
    assert context["vervolg"] == "/projects/delete/demo"
    assert context["handeling"] == "dit project te verwijderen"
    assert context["fout"] == "oeps"
    assert context["geregistreerd"] is True
    assert render.call_args.kwargs["template"] == "bg/_step-up.html.j2"


@pytest.mark.asyncio
async def test_render_step_up_dialog_zonder_factor_meldt_niet_geregistreerd() -> None:
    status = OtpStatus(registered=False, confirmed_at=None)
    with (
        patch("opi.services.user_otp_service.UserOtpService.status", new=AsyncMock(return_value=status)),
        patch("opi.web.lotc_switch.render_fragment", return_value="<dialoog/>") as render,
    ):
        await render_step_up_dialog(_request(), vervolg="/x", handeling="iets")

    assert render.call_args.kwargs["context"]["geregistreerd"] is False
    assert render.call_args.kwargs["context"]["fout"] == ""


def test_render_step_up_repeat_wijst_naar_het_vervolg() -> None:
    with patch("opi.web.lotc_switch.render_fragment", return_value="<herhaal/>") as render:
        html = render_step_up_repeat(_request(), vervolg="/projects/delete/demo")

    assert html == "<herhaal/>"
    assert render.call_args.kwargs["template"] == "bg/_step-up-herhalen.html.j2"
    assert render.call_args.kwargs["context"]["vervolg"] == "/projects/delete/demo"
    assert render.call_args.kwargs["context"]["vervolg_token"] == ""


def test_render_step_up_repeat_geeft_het_wizard_token_mee() -> None:
    """De herhaling post bodyloos, dus het token moet als body-waarde mee."""
    with patch("opi.web.lotc_switch.render_fragment", return_value="<herhaal/>") as render:
        render_step_up_repeat(_request(), vervolg="/projects/demo/x/confirm", vervolg_token="wt-123")

    assert render.call_args.kwargs["context"]["vervolg_token"] == "wt-123"


@pytest.mark.asyncio
async def test_render_step_up_dialog_geeft_het_wizard_token_mee() -> None:
    status = OtpStatus(registered=True, confirmed_at=None)
    with (
        patch("opi.services.user_otp_service.UserOtpService.status", new=AsyncMock(return_value=status)),
        patch("opi.web.lotc_switch.render_fragment", return_value="<dialoog/>") as render,
    ):
        await render_step_up_dialog(_request(), vervolg="/x", handeling="iets", vervolg_token="wt-123")

    assert render.call_args.kwargs["context"]["vervolg_token"] == "wt-123"


# ---------------------------------------------------------------------------
# Het sjabloon zelf, door de echte LOTC-laag
# ---------------------------------------------------------------------------


def _render_dialoog(*, geregistreerd: bool) -> str:
    """Het sjabloon door templates_lotc, niet door een gemockte render_fragment.

    De tests hierboven toetsen de context; deze toetst dat die context ook een
    bruikbare dialoog oplevert. Een componenttag die niet bestaat valt hier om met
    ComponentError, en een veld dat uit het sjabloon verdwijnt haalt de assert niet.
    """
    from opi.core.templates_lotc import templates_lotc

    return templates_lotc.env.get_template("bg/_step-up.html.j2").render(
        {
            "request": SimpleNamespace(state=SimpleNamespace(csrf_token="csrf")),
            "csp_nonce": "nonce",
            "vervolg": "/projects/delete/demo",
            "handeling": "dit project te verwijderen",
            "fout": "",
            "geregistreerd": geregistreerd,
            "vervolg_token": "",
        }
    )


def test_de_dialoog_vraagt_een_code_van_wie_een_factor_heeft() -> None:
    html = _render_dialoog(geregistreerd=True)
    assert 'name="code"' in html
    assert 'hx-post="/step-up"' in html
    assert 'name="vervolg" value="/projects/delete/demo"' in html


def test_de_dialoog_wijst_naar_de_registratie_zonder_factor() -> None:
    html = _render_dialoog(geregistreerd=False)
    assert 'name="code"' not in html
    assert "/account/otp" in html
