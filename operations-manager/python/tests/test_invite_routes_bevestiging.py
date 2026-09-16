"""De bedrading van de bevestigingsstap door de uitnodigingsroutes (RC-191).

Twee dingen die het sjabloon zelf niet kan bewijzen:

1. De LOKALE registratie zet ``verify_email`` in de sessie, en geeft door of de mail eruit
   ging. De SSO-tak zet die vlag niet, want die gebruiker is via ``trustEmail`` al
   geverifieerd.
2. De succespagina leest die vlag terug en geeft hem aan het sjabloon.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api.invite_routes import invite_register_submit, invite_sso_callback, invite_success

PROJECT = ("demo", {"display-name": "Demo-applicatie"}, {"contact_email": "beheer@example.org"}, "sandbox")

FORM = {
    "email": "iemand@example.org",
    "first_name": "Ie",
    "last_name": "Mand",
    "password": "GeheimGeheim1",
    "password_confirm": "GeheimGeheim1",
}


def _request(session: dict[str, Any] | None = None) -> MagicMock:
    request = MagicMock()
    request.session = session if session is not None else {}
    request.query_params = {}
    request.headers = {}
    request.form = AsyncMock(return_value=dict(FORM))
    return request


def _invite_manager(result_data: dict[str, Any]) -> MagicMock:
    manager = MagicMock()
    manager.validate_auth_method.return_value = True
    manager.complete_local_invite = AsyncMock(return_value=result_data)
    manager.project_file_handler.get_invite_success_title.return_value = "Gelukt"
    manager.project_file_handler.get_invite_success_button.return_value = "Ga naar applicatie"
    return manager


async def _submit(verification_mail_sent: bool) -> dict[str, Any]:
    request = _request()
    manager = _invite_manager(
        {
            "user_id": "u1",
            "email": "iemand@example.org",
            "created": True,
            "assigned": {"roles": [], "errors": []},
            "verification_mail_sent": verification_mail_sent,
        }
    )
    with (
        patch("opi.api.invite_routes._find_project_by_invite_key", AsyncMock(return_value=PROJECT)),
        patch("opi.api.invite_routes.InviteManager", return_value=manager),
    ):
        response = await invite_register_submit(request, "sleutel")

    assert response.status_code == 302
    return request.session["invite_success"]


@pytest.mark.asyncio
async def test_de_lokale_registratie_vraagt_de_pagina_om_de_bevestigingstekst() -> None:
    opgeslagen = await _submit(verification_mail_sent=True)

    assert opgeslagen["verify_email"] is True
    assert opgeslagen["verification_mail_sent"] is True


@pytest.mark.asyncio
async def test_een_mislukte_mail_reist_mee_naar_de_pagina() -> None:
    opgeslagen = await _submit(verification_mail_sent=False)

    assert opgeslagen["verify_email"] is True
    assert opgeslagen["verification_mail_sent"] is False


async def _success_context(success_info: dict[str, Any]) -> dict[str, Any]:
    request = _request({"invite_success": success_info})
    with (
        patch("opi.api.invite_routes._find_project_by_invite_key", AsyncMock(return_value=PROJECT)),
        patch("opi.api.invite_routes.InviteManager", return_value=_invite_manager({})),
        patch("opi.api.invite_routes.render") as render,
    ):
        await invite_success(request, "sleutel")

    return render.call_args.kwargs["context"]


@pytest.mark.asyncio
async def test_de_succespagina_krijgt_de_vlag_en_het_contactadres() -> None:
    context = await _success_context(
        {
            "email": "iemand@example.org",
            "created": True,
            "assigned": {},
            "verify_email": True,
            "verification_mail_sent": False,
        }
    )

    assert context["verify_email"] is True
    assert context["verification_mail_sent"] is False
    assert context["contact_email"] == "beheer@example.org"


@pytest.mark.asyncio
async def test_de_sso_callback_vraagt_geen_bevestigingstekst() -> None:
    """Die gebruiker komt via ``trustEmail`` al geverifieerd binnen."""
    request = _request(
        {
            "invite_flow": {
                "key": "sleutel",
                "state": "s1",
                "keycloak_url": "https://kc.example.org",
                "realm_name": "rig-demo",
                "code_verifier": "v",
                "redirect_uri": "https://x/cb",
            }
        }
    )
    request.query_params = {"state": "s1", "code": "c1"}
    manager = _invite_manager({})
    manager.complete_sso_invite = AsyncMock(
        return_value={
            "user_id": "u1",
            "email": "iemand@example.org",
            "created": False,
            "assigned": {"roles": [], "errors": []},
        }
    )
    with (
        patch("opi.api.invite_routes._find_project_by_invite_key", AsyncMock(return_value=PROJECT)),
        patch("opi.api.invite_routes.InviteManager", return_value=manager),
        patch("opi.api.invite_routes._exchange_code_for_token", AsyncMock(return_value={"access_token": "t"})),
        patch("opi.api.invite_routes._get_userinfo", AsyncMock(return_value={"email": "iemand@example.org"})),
    ):
        response = await invite_sso_callback(request, "sleutel")

    assert response.headers["location"] == "/invite/sleutel/success"
    opgeslagen = request.session["invite_success"]
    assert "verify_email" not in opgeslagen
    assert "verification_mail_sent" not in opgeslagen


@pytest.mark.asyncio
async def test_zonder_vlag_krijgt_de_succespagina_de_oude_tekst() -> None:
    """Wat de SSO-callback opslaat, geeft ``verify_email=False`` aan het sjabloon."""
    context = await _success_context({"email": "iemand@example.org", "created": True, "assigned": {}})

    assert context["verify_email"] is False
