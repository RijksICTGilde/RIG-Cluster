"""The local-only blueprint: local accounts only, no SSO Rijk."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from keycloak.exceptions import KeycloakGetError
from opi.connectors.keycloak import PLATFORM_IDP_ALIAS, KeycloakConnector
from opi.core.config import settings
from opi.forms.visualizers.providers import InviteAuthMethodOptionsProvider, KeycloakTemplateOptionsProvider
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.manager.keycloak_manager import KeycloakManager
from opi.services.catalog.invite.editables import INVITE_AUTH_METHODS_EDITABLE
from opi.services.catalog.keycloak.templates import (
    LOCAL_ONLY_TEMPLATE,
    SSO_ONLY_TEMPLATE,
    SSO_SUPPORT_TEMPLATE,
    allows_local_accounts,
    allows_sso,
)
from ruamel.yaml import YAML

BLUEPRINT = Path(__file__).parent.parent / "opi" / "configs" / "keycloak" / f"{LOCAL_ONLY_TEMPLATE}.yaml"


def _project(template: str) -> dict[str, Any]:
    return {"services": [{"name": "keycloak", "config": {"template": template}}]}


def test_local_only_allows_local_accounts_and_no_sso() -> None:
    assert allows_local_accounts(LOCAL_ONLY_TEMPLATE)
    assert not allows_sso(LOCAL_ONLY_TEMPLATE)
    assert allows_sso(SSO_SUPPORT_TEMPLATE)
    assert allows_sso(None), "an empty template is sso-only"


def test_blueprint_does_not_federate() -> None:
    blueprint = YAML(typ="safe").load(BLUEPRINT.read_text(encoding="utf-8"))

    assert "platformClients" not in blueprint
    assert "identityProviders" not in blueprint
    assert blueprint["browserFlow"] == "browser"


def test_template_choice_offers_local_only() -> None:
    values = [o["value"] for o in KeycloakTemplateOptionsProvider().get_options()]

    assert LOCAL_ONLY_TEMPLATE in values


def test_invite_offers_only_a_local_account() -> None:
    options = InviteAuthMethodOptionsProvider(_project(LOCAL_ONLY_TEMPLATE)).get_options()

    assert [o["value"] for o in options] == ["local"]


def test_invite_auth_method_field_is_hidden_when_there_is_nothing_to_choose() -> None:
    shown_for = INVITE_AUTH_METHODS_EDITABLE.show_when["value"]  # type: ignore[index]

    assert LOCAL_ONLY_TEMPLATE not in shown_for
    assert SSO_ONLY_TEMPLATE not in shown_for
    assert SSO_SUPPORT_TEMPLATE in shown_for


def _connector_with_admin(admin: MagicMock) -> KeycloakConnector:
    connector = KeycloakConnector.__new__(KeycloakConnector)
    connector.admin = admin
    return connector


async def test_delete_identity_provider_deletes_an_existing_one() -> None:
    admin = MagicMock()
    admin.get_idp.return_value = {"alias": PLATFORM_IDP_ALIAS}

    deleted = await _connector_with_admin(admin).delete_identity_provider("rig-demo", PLATFORM_IDP_ALIAS)

    assert deleted
    admin.delete_idp.assert_called_once_with(idp_alias=PLATFORM_IDP_ALIAS)
    admin.change_current_realm.assert_called_with("master")


async def test_delete_identity_provider_is_a_noop_when_absent() -> None:
    admin = MagicMock()
    admin.get_idp.side_effect = KeycloakGetError("not found", response_code=404)

    deleted = await _connector_with_admin(admin).delete_identity_provider("rig-demo", PLATFORM_IDP_ALIAS)

    assert not deleted
    admin.delete_idp.assert_not_called()


@pytest.mark.parametrize(("template", "teardown"), [(LOCAL_ONLY_TEMPLATE, True), (SSO_SUPPORT_TEMPLATE, False)])
async def test_switching_to_local_only_removes_idp_and_platform_client(template: str, teardown: bool) -> None:
    keycloak = MagicMock()
    keycloak.delete_identity_provider = AsyncMock(return_value=True)
    keycloak.delete_client_by_client_id = AsyncMock(return_value=True)

    with (
        patch("opi.manager.keycloak_manager.create_keycloak_connector", new=AsyncMock(return_value=keycloak)),
        patch.object(KeycloakYamlHandler, "ensure_identity_providers", new=AsyncMock()),
    ):
        await KeycloakManager(Mock())._ensure_realm_identity_providers(
            "demo", "odcn-production", "demo-odcn-production", "https://keycloak.example", {"template": template}
        )

    if teardown:
        keycloak.delete_identity_provider.assert_awaited_once_with("demo-odcn-production", PLATFORM_IDP_ALIAS)
        keycloak.delete_client_by_client_id.assert_awaited_once_with(
            settings.KEYCLOAK_DEFAULT_REALM, "demo-odcn-production-platform"
        )
    else:
        keycloak.delete_identity_provider.assert_not_awaited()
        keycloak.delete_client_by_client_id.assert_not_awaited()
