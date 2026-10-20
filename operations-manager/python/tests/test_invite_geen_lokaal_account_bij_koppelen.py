"""Geen lokaal account via een uitnodiging in een realm dat op e-mailadres koppelt (RC-297).

Zo'n realm (``account-link: automatic`` of ``confirm``) koppelt een brokered SSO-identiteit
aan elk account met hetzelfde adres. De lokale uitnodigingsweg laat de uitgenodigde dat adres
zelf intypen, dus daar hoort die weg dicht te zijn. Dit meet de drie plekken waar dat moet
gebeuren: de uitnodigingsweg zelf, de weigering bij opslaan en de keuzelijst in het formulier.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.visualizers.providers import InviteAuthMethodOptionsProvider
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_validation import validate_project_structure

INVITE = {"key": "sleutel", "realm-roles": ["allowed-user"]}


def _project(account_link: str | None, auth_methods: list[str] | None = None) -> dict[str, Any]:
    keycloak: dict[str, Any] = {"template": "sso-support"}
    if account_link is not None:
        keycloak["account-link"] = account_link
    invite = dict(INVITE)
    if auth_methods is not None:
        invite["auth-methods"] = auth_methods
    return {
        "schema-version": 2,
        "name": "demo",
        "services": [
            {"name": "keycloak", "config": keycloak},
            {"name": "invite", "config": {"active": [invite]}},
        ],
        "components": [{"name": "api", "type": "single"}],
        "deployments": [{"name": "deployment-1", "cluster": "local", "namespace": "demo", "components": []}],
    }


def _active_invite(project: dict[str, Any]) -> dict[str, Any]:
    return ProjectFileHandler().get_all_active_invites(project)[0]


class TestDeUitnodigingswegZelf:
    @pytest.mark.parametrize("auth_methods", [None, ["sso", "local"], ["local"]])
    def test_een_koppelend_realm_biedt_geen_lokale_weg(self, auth_methods: list[str] | None) -> None:
        """Wat de uitnodiging ook zegt: het realm beslist, dus ook een uitnodiging die
        ``local`` expliciet noemt krijgt hem hier niet."""
        project = _project("automatic", auth_methods)
        methods = ProjectFileHandler().get_invite_auth_methods(project, _active_invite(project))

        assert methods["local"] is False

    @pytest.mark.parametrize(("auth_methods", "local"), [(None, True), (["sso", "local"], True), (["sso"], False)])
    def test_zonder_koppelen_blijft_de_uitnodiging_beslissen(self, auth_methods: list[str] | None, local: bool) -> None:
        project = _project(None, auth_methods)
        methods = ProjectFileHandler().get_invite_auth_methods(project, _active_invite(project))

        assert methods["local"] is local

    @pytest.mark.asyncio
    async def test_de_registratieroute_maakt_geen_account_aan(self) -> None:
        """De POST komt niet voorbij ``validate_auth_method``, dus Keycloak wordt niet
        benaderd. Gemeten op de connector, want die maakt het account."""
        from opi.api.invite_routes import invite_register_submit

        project = _project("automatic")
        request = MagicMock()
        request.session = {}
        request.query_params = {}
        request.headers = {}
        request.form = AsyncMock(
            return_value={
                "email": "iemand@example.org",
                "first_name": "Ie",
                "last_name": "Mand",
                "password": "GeheimGeheim1",
                "password_confirm": "GeheimGeheim1",
            }
        )
        keycloak = MagicMock()
        keycloak.create_user = AsyncMock()

        with (
            patch(
                "opi.api.invite_routes._find_project_by_invite_key",
                AsyncMock(return_value=("demo", project, _active_invite(project), "sandbox")),
            ),
            patch("opi.manager.invite_manager.create_keycloak_connector", AsyncMock(return_value=keycloak)),
        ):
            response = await invite_register_submit(request, "sleutel")

        assert response.status_code == 200
        assert "Authenticatiemethode niet beschikbaar" in response.body.decode()
        keycloak.create_user.assert_not_called()


class TestDeWeigeringBijOpslaan:
    def test_local_in_auth_methods_mag_niet_samen_met_koppelen(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="koppelt automatisch op e-mailadres"):
            asyncio.run(validate_project_structure(_project("automatic", ["local"])))

    def test_zonder_koppelen_komt_dezelfde_uitnodiging_erdoor(self) -> None:
        asyncio.run(validate_project_structure(_project(None, ["local"])))

    def test_koppelen_zonder_lokale_uitnodiging_komt_erdoor(self) -> None:
        asyncio.run(validate_project_structure(_project("automatic", ["sso"])))


class TestDeKeuzelijstInHetFormulier:
    def test_de_wizard_biedt_lokaal_account_niet_aan(self) -> None:
        """De wizard houdt de dienstconfig onder zijn eigen root, met in ``services`` alleen
        de gekozen namen. Een lezing die alleen ``services`` kent vond de koppelstand daar
        niet en bood ``local`` alsnog aan, met de fout pas bij het opslaan."""
        wizard = {
            "services": ["keycloak", "invite"],
            "_services-config": [
                {"name": "keycloak", "config": {"template": "sso-support", "account-link": "automatic"}}
            ],
        }
        options = InviteAuthMethodOptionsProvider(yaml_data=wizard).get_options()

        assert [option["value"] for option in options] == ["sso"]

    def test_in_de_wizard_zonder_koppelen_blijft_local_staan(self) -> None:
        wizard = {
            "services": ["keycloak", "invite"],
            "_services-config": [{"name": "keycloak", "config": {"template": "sso-support"}}],
        }
        options = InviteAuthMethodOptionsProvider(yaml_data=wizard).get_options()

        assert [option["value"] for option in options] == ["sso", "local"]

    def test_het_projectbestand_leest_dezelfde_stand(self) -> None:
        options = InviteAuthMethodOptionsProvider(yaml_data=_project("confirm")).get_options()

        assert [option["value"] for option in options] == ["sso"]
